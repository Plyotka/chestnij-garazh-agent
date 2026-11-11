# -*- coding: utf-8 -*-
"""
Разбор звонков «Честный Гараж» — Yandex Cloud Serverless Function.
Облачный триггер: GitHub Actions каждые 30 минут, окно 9:00–20:00 мск.
Транскрипцию звонков делает «Войси» (заметки уже в карточках amo).
Функция: разбирает свежие расшифровки, называет сделку/контакт,
маршрутизирует по схеме Бадри/Константин, ставит задачи, пишет разбор.

Переменные окружения (задаются в версии функции):
  AMO_TOKEN          — Bearer-токен amoCRM (обязательно)
  MOONSHOT_API_KEY   — ключ Kimi API (обязательно)
  AMO_BASE           — по умолчанию https://autoservice61.amocrm.ru
  KIMI_BASE          — по умолчанию https://api.moonshot.ai
  KIMI_MODEL         — по умолчанию kimi-k2.6 (только temperature=1)
  MAX_LEADS_PER_RUN  — мягкий лимит сделок за прогон (по умолчанию 30)
  DRY_RUN            — "true" = только лог, без записи в amo (для теста)
"""
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

AMO_BASE = os.environ.get("AMO_BASE", "https://autoservice61.amocrm.ru").rstrip("/")
AMO_TOKEN = os.environ.get("AMO_TOKEN", "")
KIMI_BASE = os.environ.get("KIMI_BASE", "https://api.moonshot.ai").rstrip("/")
KIMI_KEY = os.environ.get("MOONSHOT_API_KEY", "")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2.6")
MAX_LEADS_PER_RUN = int(os.environ.get("MAX_LEADS_PER_RUN", "30"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"

# Люди
BADRI = 14367338
KONSTANTIN = 14221122
# Статусы воронки 11282498
ST_NERAZOB = 88470410      # Неразобранное
ST_FIRST = 88470414        # Первичный контакт
ST_TEPLY = 88470418        # Тёплые
ST_PRIEDET = 88470422      # приедет
ST_PRIEHAL = 88470426      # приехал
ST_SUCCESS = 142           # Успешно
PHONE_FIELD = 1133163      # поле «Телефон» в контактах (code PHONE, multitext)
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-(]*\d{3}[\s\-)]*[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
ST_CLOSED = 143            # Закрыто не реализовано
LOSS_NE_NASH_PROFIL = 24763846
LOSS_NE_DOZVON = 24853446  # Не дозвонились
LOSS_DUBL = 24765102       # Дубль
LOSS_PROPALA = 24393498    # Пропала потребность
LOSS_DRUGIE = 24393506     # Выбрали других
# отказ/неактуальность КЛИЕНТА (для причины закрытия и детерминированной страховки)
CLIENT_REFUSED_RE = re.compile(
    r"уже не\s?актуально|уже не\s?нужн|больше не\s?нужн|уже (сделал|обслуж|отремонтир|отказ)"
    r"|сделал(а)? (у|в) других|передумал(а)?|отказал(ся|ась|ись)? от (визита|записи|услуги)"
    r"|отпад|неинтересно", re.IGNORECASE)
TAG_CALL = "Звонок"  # тег сделок звонкового контура

STAGE_RANK = {ST_NERAZOB: 0, ST_FIRST: 1, ST_TEPLY: 2, ST_PRIEDET: 3, ST_PRIEHAL: 4}

# Войси с ~11.2026 вставляет в тексты расшифровок невидимые символы
# (zero-width spaces, BOM, word joiner и пр.). Кириллица на месте, но сырые
# тексты отравляют LLM и ломает детерминированные regex-проверки — чистим
# перед разбором (в хранимые заметки не лезем, только вход классификатора).
ZW_RE = re.compile(r"[\u200b\u200c\u200d\u2060\u2063\ufeff\u200e\u200f]")


def clean_text(t):
    if not t:
        return t or ""
    t = ZW_RE.sub("", t)
    return re.sub(r"[\u00a0\u2007\u2009\u202f\u205f]", " ", t)


def log(msg):
    print(f"[{datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}] {msg}", flush=True)


def http(method, url, token=None, body=None, tries=3, timeout=40):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last_err = None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "chg-razbor/1.0")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            last_err = f"HTTP {e.code}: {detail}"
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {last_err}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
            if i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {last_err}")
    raise RuntimeError(last_err or "unknown http error")


def amo_get(path):
    return http("GET", f"{AMO_BASE}{path}", token=AMO_TOKEN)


def amo(method, path, body):
    return http(method, f"{AMO_BASE}{path}", token=AMO_TOKEN, body=body)


def note_text(n):
    p = n.get("params") or {}
    return (p.get("text") or p.get("message") or "").strip()


def embedded(resp, key):
    return (resp.get("_embedded") or {}).get(key) or []


# ---------------- Kimi ----------------

CLASSIFY_SYSTEM = """Ты — оператор сделок автосервиса «Честный Гараж». По расшифровке одного звонка определи категорию и верни СТРОГО один JSON-объект (без пояснений и markdown).

КАТЕГОРИИ:
1 — нет ясности ПО СУТИ (правило 28.10: это Бадри): недозвон, не дозвонились, клиент просил перезвонить, «сам перезвоню», а также звонок-пустышка: поговорили ни о чём, обрыв связи, недоговорили, НЕпонятно какая машина или какая проблема. Если суть (машина/проблема) непонятна — всегда категория 1, даже если разговор длинный.
2 — ясно ЧТО нужно (машина и/или проблема определены), но НЕ ясно, приедет ли: тёплый интерес, думает, сравнивает, сказал «позвоню обратно».
3 — ясно ЧТО нужно И когда приедет: договорились о дате/времени приезда. ВАЖНО: если клиент сказал «приеду/подъеду завтра (утром)», «заеду сегодня», «подъеду в понедельник» — это категория 3, ДАЖЕ ЕСЛИ добавил «перезвоню подтвердить/уточнить время». Названный день приезда = категория 3.
4 — клиент УЖЕ приехал / авто на сервисе / работа в процессе.
5 — ОТКАЗ/ЗАКРЫТИЕ, любой из двух случаев:
  а) сервис отказал (не делаем такие работы / такое авто / нет запчастей и клиент уходит);
  б) КЛИЕНТ ОТКАЗАЛСЯ: обращение «уже неактуально», «сделал у других», «передумал», «отказался от визита», «больше не нужно». Такие звонки НЕ уходят в 1 и 2 — это категория 5, сделка закрывается.

ПРАВИЛА:
- ПРАВИЛА:
- Сомнение между категориями → выбирай меньшую (1 или 2).
- Если расшифровок несколько — это НЕСКОЛЬКО звонков по одной сделке: учитывай ВСЕ вместе. Более поздний короткий или пустой звонок НЕ отменяет информацию более раннего содержательного: имя, категорию, этап и задачу определяй по совокупности, при конфликте — по более содержательному звонку.
- Имя сделки lead_name: «<Марка Модель> — <суть не более 6 слов>», например «Renault Duster — замена глушителя». Марка неизвестна → «Авто — <суть>». ВАЖНО: если в запросе передано ТЕКУЩЕЕ ИМЯ СДЕЛКИ и оно уже содержательное (не «Входящий звонок…», не «Пропущенный…», не «MAX — …», не пустое) — верни его БЕЗ ИЗМЕНЕНИЙ, даже если расшифровка новая. Новое имя придумывай только если текущее пустое/системное, либо если звонок полностью меняет суть обращения.
- arrival: только для категории 3 — unixtime времени приезда по Europe/Moscow. Если сказан день недели без даты («в субботу», «в понедельник») — ближайший предстоящий такой день; время не сказано — 9:00. Иначе null.
- quality: оценка работы сотрудника с телефоном — начинается со слова «хорошо» / «средне» / «слабо», далее тире и одна фраза.
- close_reason: только для категории 5 — краткая причина отказа, иначе null.
- Все тексты на русском, без эмодзи.

Формат ответа:
{"category": 1, "lead_name": "...", "car": "...", "request": "...", "outcome": "одна фраза итога", "quality": "средне — одна фраза", "action": "одна строка действия", "arrival": null, "close_reason": null}"""


def classify(transcript, current_name=""):
    user = "РАСШИФРОВКИ ЗВОНКОВ ПО СДЕЛКЕ (по времени):\n" + transcript[:20000]
    if current_name:
        user += f"\n\nТЕКУЩЕЕ ИМЯ СДЕЛКИ: «{current_name}»"
    body = {
        "model": KIMI_MODEL,
        # kimi-k2.6 (reasoning) принимает только temperature=1
        "temperature": float(os.environ.get("KIMI_TEMPERATURE", "1")),
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    resp = http("POST", f"{KIMI_BASE}/v1/chat/completions", token=KIMI_KEY, body=body, tries=3, timeout=120)
    content = resp["choices"][0]["message"]["content"]
    return json.loads(content)


# ---------------- время ----------------

def today_18_msk():
    """Дедлайн 'к концу рабочего дня'. После 18:00 — следующее рабочее утро (10:00),
    чтобы ночные прогоны не ставили уже просроченные задачи.
    РЕГЛАМЕНТ (владелец 06.11): звонки только в будни — если дедлайн выпал на
    субботу/воскресенье, переносим на понедельник 10:00."""
    now = datetime.now(MSK)
    d = now.date()
    ts = datetime(d.year, d.month, d.day, 18, 0, tzinfo=MSK)
    if now > ts:
        ts += timedelta(days=1)
        ts = ts.replace(hour=10, minute=0)
    while ts.weekday() >= 5:  # 5=суббота, 6=воскресенье
        ts += timedelta(days=1)
        ts = ts.replace(hour=10, minute=0)
    return int(ts.timestamp())


def tomorrow_10_msk():
    """10:00 следующего РАБОЧЕГО дня (пн–пт) — субботу и воскресенье пропускаем
    (регламент владельца 06.11: в выходные не звоним)."""
    d = datetime.now(MSK).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, 10, 0, tzinfo=MSK).timestamp())


# ---------------- задачи ----------------

# ключ действия задачи — по нему понимаем, что это «та же» задача
TASK_KEYS = ("Перезвонить/актуализировать", "Когда приедет?", "Приехал?",
             "Актуализировать обращение", "отправить клиенту")


def task_key(text):
    for k in TASK_KEYS:
        if k in text:
            return k
    return text[:30]


def upsert_task(lead_id, text, responsible_id, complete_till):
    """Правило владельца 06.11: закрытые задачи НИКОГДА не редактируются и не
    воскрешаются (запросы ниже фильтруют is_completed=0). Если нужное действие
    уже есть открытым — обновляем его (ответственный/срок/текст). Если действие
    сменилось — старую открытую ЗАКРЫВАЕМ (не редактируем), создаём НОВУЮ.
    Если ничего нет — создаём новую."""
    resp = amo_get(
        f"/api/v4/tasks?filter[entity_id]={lead_id}&filter[entity_type][]=leads&filter[is_completed]=0&limit=100"
    )
    open_tasks = embedded(resp, "tasks")
    mine = [t for t in open_tasks if (t.get("text") or "").startswith("Kimi для")]
    want = task_key(text)
    same = [t for t in mine if task_key(t.get("text") or "") == want]
    if same:
        amo("PATCH", "/api/v4/tasks", [
            {"id": t["id"], "text": text, "responsible_user_id": responsible_id, "complete_till": complete_till}
            for t in same
        ])
        return
    # действие сменилось или задач нет: старые открытые закрываем, новую создаём
    if mine:
        amo("PATCH", "/api/v4/tasks", [{"id": t["id"], "is_completed": True} for t in mine])
    amo("POST", "/api/v4/tasks", [{
        "text": text,
        "responsible_user_id": responsible_id,
        "complete_till": complete_till,
        "entity_id": lead_id,
        "entity_type": "leads",
    }])


def close_my_tasks(lead_id):
    resp = amo_get(
        f"/api/v4/tasks?filter[entity_id]={lead_id}&filter[entity_type][]=leads&filter[is_completed]=0&limit=100"
    )
    mine = [t for t in embedded(resp, "tasks") if (t.get("text") or "").startswith("Kimi для")]
    if mine:
        amo("PATCH", "/api/v4/tasks", [{"id": t["id"], "is_completed": True} for t in mine])


def carry_overdue_tasks(now):
    """ШАГ 0 (правка 07.11 по просьбе владельца): просроченная задача ВИСИТ красной
    до конца рабочего дня — перенос на завтра 10:00 мск происходит только в прогонах
    после 19:00 мск; дневные прогоны просроченные не трогают.
    Сделка в 142/143 → закрыть задачу (всегда). Текст, ответственного и связь не менять."""
    now_ts = int(now.timestamp())
    move_overdue = now.hour >= 19  # мск — now приходит в MSK (run(): datetime.now(MSK))
    due = tomorrow_10_msk()

    moved, closed = [], []
    url = f"{AMO_BASE}/api/v4/tasks?filter[is_completed]=0&filter[complete_till][to]={now_ts}&limit=250"
    while url:
        resp = http("GET", url, token=AMO_TOKEN)
        tasks = embedded(resp, "tasks")
        lead_ids = sorted({t["entity_id"] for t in tasks
                           if t.get("entity_type") == "leads" and t.get("entity_id")})
        statuses = {}
        for i in range(0, len(lead_ids), 50):
            ids = lead_ids[i:i + 50]
            qs = "&".join(f"filter[id][]={x}" for x in ids)
            lr = amo_get(f"/api/v4/leads?{qs}&limit=250")
            for l in embedded(lr, "leads"):
                statuses[l["id"]] = l.get("status_id")
        patch_move, patch_close = [], []
        for t in tasks:
            if t.get("entity_type") != "leads" or not t.get("entity_id"):
                continue  # задачи вне сделок не трогаем
            if statuses.get(t["entity_id"]) in (ST_SUCCESS, ST_CLOSED):
                patch_close.append({"id": t["id"], "is_completed": True})
            elif move_overdue:
                patch_move.append({"id": t["id"], "complete_till": due})
        if DRY_RUN:
            log(f"  DRY_RUN шаг 0: перенести {len(patch_move)}, закрыть {len(patch_close)}"
                f"{' (перенос выкл: ещё не 19:00 мск)' if not move_overdue else ''}")
        else:
            if patch_move:
                amo("PATCH", "/api/v4/tasks", patch_move)
            if patch_close:
                amo("PATCH", "/api/v4/tasks", patch_close)
        moved.extend(t["id"] for t in patch_move)
        closed.extend(t["id"] for t in patch_close)
        nxt = (resp.get("_links") or {}).get("next") or {}
        url = nxt.get("href")
    log(f"шаг 0: перенесено {len(moved)}, закрыто {len(closed)} просроченных задач")
    return {"moved": len(moved), "closed": len(closed)}


# ---------------- лоток «Неразобранное» (unsorted) ----------------

def norm_phone(raw):
    """11 цифр, начинается с 7/8 -> +7XXXXXXXXXX, иначе None."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return None


def repair_phones(leads):
    """Сделки с номером в имени, но без телефона в контакте, — дозаписать номер.
    Иначе дубли по номеру не видны: имя может переименоваться, а контакт остаться пустым."""
    fixed = 0
    for l in leads:
        if l.get("status_id") in (ST_SUCCESS, ST_CLOSED):
            continue
        phone = extract_phone(l.get("name") or "")
        if not phone:
            continue
        full = amo_get(f"/api/v4/leads/{l['id']}?with=contacts")
        contacts = embedded(full, "contacts")
        if not contacts:
            # контакта нет вовсе — создаём и привязываем к сделке через /link
            # (_embedded при создании контакта сделку НЕ привязывает — проверено эмпирикой)
            created = amo("POST", "/api/v4/contacts", [{
                "name": l.get("name") or f"Клиент {phone}",
                "custom_fields_values": [{"field_id": PHONE_FIELD, "values": [{"value": phone}]}]}])
            new_cid = ((created.get("_embedded") or {}).get("contacts") or [{}])[0].get("id")
            if new_cid:
                amo("POST", f"/api/v4/leads/{l['id']}/link",
                    [{"to_entity_id": new_cid, "to_entity_type": "contacts"}])
            fixed += 1
            log(f"  контакт с номером создан и привязан: сделка {l['id']} ({phone})")
            continue
        payload = []
        for c in contacts:
            try:
                detail = amo_get(f"/api/v4/contacts/{c['id']}")
                values = detail.get("custom_fields_values") or []
                has_phone = any(v.get("field_id") == PHONE_FIELD and v.get("values") for v in values)
            except Exception:
                has_phone = True  # не смогли проверить — не трогаем
            if not has_phone:
                payload.append({"id": c["id"], "custom_fields_values": [
                    {"field_id": PHONE_FIELD, "values": [{"value": phone}]}]})
        if payload:
            amo("PATCH", "/api/v4/contacts", payload)
            fixed += len(payload)
            log(f"  телефон из имени дозаписан в контакт: сделка {l['id']} ({phone})")
    if fixed:
        log(f"ремонт телефонов: дозаписано {fixed} контактам")
    return fixed


def find_open_lead_by_phone(phone):
    """Открытая сделка с таким номером (для дедупликации пропущенных).
    Ищем по цифрам в имени сделки, а если не нашли — по контактам с таким
    телефоном (кейсы 06.11: сделка переименована содержательно, номер живёт
    только в контакте — имя «Suzuki Alto — промывка печки» не содержит цифр,
    и старая проверка по имени дубль не видела)."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    resp = amo_get(f"/api/v4/leads?query={digits}&limit=20")
    for l in embedded(resp, "leads"):
        if l.get("status_id") in (ST_SUCCESS, ST_CLOSED):
            continue
        if digits in re.sub(r"\D", "", l.get("name") or ""):
            return l["id"]
    # fallback: контакт с этим номером → его открытые сделки
    try:
        cands = embedded(amo_get(f"/api/v4/contacts?query={digits}&limit=10"), "contacts")
        for c in cands:
            detail = amo_get(f"/api/v4/contacts/{c['id']}?with=leads")
            vals = detail.get("custom_fields_values") or []
            has = any(
                v.get("field_id") == PHONE_FIELD
                and digits in re.sub(r"\D", "", str((val or {}).get("value") or ""))
                for v in vals for val in (v.get("values") or [])
            )
            if not has:
                continue
            for ll in embedded(detail, "leads"):
                if ll.get("status_id") not in (ST_SUCCESS, ST_CLOSED):
                    return ll["id"]
    except Exception:
        pass
    return None


def process_unsorted(now):
    """Лоток «Неразобранное» (входящие интеграций): пропущенные звонки телефонии и
    диалоги MAX принимаем в воронку с маршрутизацией (Бадри + задача + маркер).
    Чаты Авито тут не принимаем — их разбирает chats.py после ответа Бадри.
    Прочие заявки лотка (формы) не трогаем — их разбирает менеджер вручную."""
    items = embedded(amo_get("/api/v4/leads/unsorted?limit=200"), "unsorted")
    handled = 0
    for it in items:
        src = it.get("source_name") or ""
        meta = it.get("metadata") or {}
        uid = it.get("uid")
        try:
            if "пропущен" in src.lower():
                phone = norm_phone(str(meta.get("from") or ""))
                dup = find_open_lead_by_phone(phone)
                if dup:
                    # повторный звонок с того же номера — дубль не плодим.
                    # Задача перезвона: только если свежий разбор НЕ говорит, что
                    # клиент пообещал перезвонить сам (кейс Патриот 11.11: клиент
                    # дозвонился, получил цену и «перезвонит сам» — повторные
                    # пропущенные от него НЕ должны плодить задачи на сегодня).
                    wait = False
                    try:
                        recent = embedded(amo_get(
                            f"/api/v4/leads/{dup}/notes?limit=30&order[created_at]=desc"), "notes")
                        cutoff = now.timestamp() - 36 * 3600
                        fresh = [note_text(n) for n in recent
                                 if (n.get("created_at") or 0) >= cutoff]
                        joined = " ".join(fresh).lower()
                        wait = bool(re.search(
                            r"перезвонит сам|пообещал перезвонить|сам перезвонит|"
                            r"сам набер|позвонит, если|обещал позвонить", joined))
                    except Exception:
                        wait = False
                    if not DRY_RUN:
                        amo("POST", f"/api/v4/leads/{dup}/notes", [{"note_type": "common", "params": {
                            "text": f"Повторный пропущенный звонок {phone} ({now.strftime('%d.%m.%Y %H:%M')}) — дубль не создан"}}])
                        if wait:
                            log(f"  лоток: пропущенный {phone} — открытая сделка {dup}, клиент обещал перезвонить сам — задача НЕ поставлена")
                        else:
                            upsert_task(dup, f"Kimi для Бадри: Перезвонить/актуализировать по пропущенному: {phone}",
                                        BADRI, today_18_msk())
                            log(f"  лоток: пропущенный {phone} — открытая сделка {dup}, дубль не создан, задача перезвона добавлена")
                        # F21 (кейс 11.11): заявку в лотке API отклонить не даёт
                        # (405 — платформенная ловушка) и она висела мусором.
                        # Принимаем пустым телом и СРАЗУ закрываем «Дубль» —
                        # лоток чист, дубль-сделка в закрытых с причиной.
                        try:
                            acc = amo("POST", f"/api/v4/leads/unsorted/{uid}/accept", {})
                            dup_lid = ((acc.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
                            if dup_lid:
                                amo("PATCH", "/api/v4/leads", [{"id": dup_lid,
                                     "name": f"Дубль — пропущенный {phone}",
                                     "status_id": ST_CLOSED, "loss_reason_id": LOSS_DUBL}])
                                amo("POST", f"/api/v4/leads/{dup_lid}/notes", [{"note_type": "common", "params": {
                                    "text": f"Дубль: по номеру {phone} уже есть открытая сделка {dup} "
                                            f"(задача перезвона там). Принято из лотка и закрыто "
                                            f"автоматически — иначе заявка не удаляется (API 405)."}}])
                                log(f"  лоток: заявка {str(uid)[:8]}… принята и закрыта как дубль")
                        except Exception as e2:
                            log(f"  лоток: не удалось перенести дубль в закрытые ({e2})")
                    else:
                        log(f"  DRY_RUN лоток: пропущенный {phone} — дубль, wait={wait}")
                    handled += 1
                    continue
                handled += accept_missed(uid, src, phone, meta, now)
            elif "integration.max" in src.lower() or meta.get("service") == "integration.max":
                handled += accept_max(uid, meta, now)
        except Exception as e:
            log(f"  лоток: ошибка обработки {str(uid)[:8]}…: {e}")
    if handled:
        log(f"лоток Неразобранное: обработано {handled}")
    return handled


def accept_max(uid, meta, now):
    """Диалог MAX из лотка (интеграция amojo): принять в воронку, Бадри, задача, маркер.
    Телефона в метаданных MAX обычно нет — контакт без номера."""
    if DRY_RUN:
        log(f"  DRY_RUN: unsorted MAX {str(uid)[:8]}… {(meta.get('client') or {}).get('name')}")
        return 1
    acc = amo("POST", f"/api/v4/leads/unsorted/{uid}/accept", {})
    new_id = ((acc.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
    if not new_id:
        raise RuntimeError("accept не вернул id сделки")
    client = ((meta.get("client") or {}).get("name") or meta.get("from") or "").strip()
    short = f"MAX — {client}" if client else "MAX — обращение"
    log(f"  лоток: принят диалог MAX ({client or 'без имени'}) -> сделка {new_id}")
    amo("PATCH", "/api/v4/leads", [{"id": new_id, "name": short, "responsible_user_id": BADRI,
        "_embedded": {"tags": [{"name": TAG_CALL}]}}])
    amo("POST", f"/api/v4/leads/{new_id}/notes", [{"note_type": "common", "params": {
        "text": f"MAX-обращение принято: {now.strftime('%d.%m.%Y')}"}}])
    upsert_task(new_id, f"Kimi для Бадри: Актуализировать обращение из MAX — {client or 'клиент'}",
                BADRI, today_18_msk())
    return 1


def accept_missed(uid, src_name, phone, meta, now):
    """Принять один пропущенный из лотка в воронку и обработать по правилу пропущенных.
    accept принимает пустое тело: сделка создаётся с именем телефонии в «Первичный»."""
    if DRY_RUN:
        log(f"  DRY_RUN: unsorted {str(uid)[:8]}… {src_name}")
        return 1
    acc = amo("POST", f"/api/v4/leads/unsorted/{uid}/accept", {})
    new_id = ((acc.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
    if not new_id:
        raise RuntimeError("accept не вернул id сделки")
    short = f"Пропущенный звонок {phone or (meta.get('from') or '')}"
    log(f"  лоток: принят пропущенный {meta.get('from')} -> сделка {new_id}")
    # короткое имя — иначе телефон в тексте задачи склеится с номером АТС
    amo("PATCH", "/api/v4/leads", [{"id": new_id, "name": short,
        "_embedded": {"tags": [{"name": TAG_CALL}]}}])
    if phone:
        amo("POST", "/api/v4/contacts", [{"name": short, "custom_fields_values": [
            {"field_id": PHONE_FIELD, "values": [{"value": phone}]}],
            "_embedded": {"leads": [{"id": new_id}]}}])
    process_missed({"id": new_id, "name": short, "status_id": ST_FIRST}, [], now)
    return 1


# ---------------- разбор ----------------

def extract_phone(text):
    """Найти российский номер в тексте (расшифровка/имя) и привести к +7XXXXXXXXXX.
    Имена телефонии часто «Входящий 79001234567» без плюса — ловим и такие."""
    m = PHONE_RE.search(text or "")
    if not m:
        m = re.search(r"(?<!\d)(7\d{10})(?!\d)", text or "")
        if not m:
            return None
        return "+7" + m.group(1)[1:]
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return "+" + digits if digits.startswith("7") or digits.startswith("8") else digits


def sync_contacts(lid, lead_name, phone_guess):
    """Имя сделки — в контакты + телефон, если в контакте его нет (правило 28.10:
    из «Неразобранного» контакт уходит без номера — дозаписываем)."""
    full = amo_get(f"/api/v4/leads/{lid}?with=contacts")
    contacts = embedded(full, "contacts")
    if not contacts:
        return
    payload = []
    for c in contacts:
        item = {"id": c["id"], "name": lead_name}
        try:
            detail = amo_get(f"/api/v4/contacts/{c['id']}")
            values = detail.get("custom_fields_values") or []
            has_phone = any(v.get("field_id") == PHONE_FIELD and v.get("values") for v in values)
        except Exception:
            has_phone = True  # не смогли проверить — не трогаем поле
        if not has_phone and phone_guess:
            item["custom_fields_values"] = [{"field_id": PHONE_FIELD, "values": [{"value": phone_guess}]}]
        payload.append(item)
    if payload:
        amo("PATCH", "/api/v4/contacts", payload)


def contact_phone(lid):
    """Телефон из контакта сделки — для текста задачи, когда из расшифровки/имени
    номер не вытащился (просьба владельца 06.11: у Бадри из списка задач
    звонить сразу, не заходя в сделку)."""
    try:
        contacts = embedded(amo_get(f"/api/v4/leads/{lid}?with=contacts"), "contacts")
        for c in contacts:
            detail = amo_get(f"/api/v4/contacts/{c['id']}")
            for v in detail.get("custom_fields_values") or []:
                if v.get("field_id") == PHONE_FIELD:
                    for val in v.get("values") or []:
                        p = (val.get("value") or "").strip()
                        if p:
                            return p
    except Exception:
        pass
    return None


def build_note(r, now):
    lines = [
        f"Разбор звонка: {now.strftime('%d.%m.%Y %H:%M')}",
        f"Авто: {r.get('car') or '—'}",
        f"Запрос: {r.get('request') or '—'}",
        f"Итог: категория {r.get('category')} — {r.get('outcome') or ''}",
        f"Приём: {r.get('quality') or '—'}",
        f"Действие: {r.get('action') or '—'}",
    ]
    if r.get("category") == 5 and r.get("close_reason"):
        lines.append(f"Причина закрытия: {r['close_reason']}")
    return "\n".join(lines)


def apply_result(lead, r, now, phone_guess=None, transcript=""):
    lid = lead["id"]
    cat = r.get("category")
    if cat not in (1, 2, 3, 4, 5):
        raise RuntimeError(f"плохая категория от LLM: {cat!r}")
    # детерминированное повышение 2→3 (кейс Audi Q5 07.11: клиент сказал
    # «приедет завтра утром», LLM дважды ставил 2 из-за «перезвонит подтвердить»)
    COME_RE = re.compile(
        r"(приеду|подъеду|заеду|привезу|подъедет|приедет|заедет|пригоню|загоню)"
        r"[^.!?]{0,50}(сегодня|завтра|послезавтра|утр|днём|вечером|в понедельник|"
        r"во вторник|в среду|в четверг|в пятницу|в субботу|в воскресенье|"
        r"к \d{1,2}[:.]\d{0,2}|на \d{1,2}[:\s]?\d{0,2})|"
        r"(сегодня|завтра|послезавтра)[^.!?]{0,40}"
        r"(приеду|подъеду|заеду|привезу|пригоню|загоню)", re.IGNORECASE)
    tail = " ".join(str(r.get(k) or "") for k in ("outcome", "action")) \
        + "\n" + (transcript or "")[-1500:]
    if cat == 2 and COME_RE.search(tail):
        cat = 3
        r = dict(r, category=3)
        r["outcome"] = (r.get("outcome") or "") + " [повышено до 3: назван день приезда]"
    # F12-страховка: LLM (temperature=1) дал 1 или 2, но в свежей расшифровке
    # клиент сам закрыл вопрос («уже неактуально» — кейс «печка не дует» 11.11:
    # модель даже написала «клиент отказался», но присвоила категорию 1).
    if cat in (1, 2) and CLIENT_REFUSED_RE.search((transcript or "")[-1200:]):
        cat = 5
        r = dict(r, category=5)
        r["close_reason"] = ((r.get("close_reason") or "") +
                             " [повышено до 5: клиент отказался в расшифровке]")
    lead_name = (r.get("lead_name") or "").strip() or lead.get("name") or f"Сделка {lid}"

    cur_status = lead.get("status_id")
    patch = {"id": lid, "name": lead_name}
    task_text = task_due = resp_id = None

    # контекст для задачи в стиле «старого» формата (похвалил владелец):
    # «Машина (телефон): суть». Телефон — вытащенный из расшифровки/имени.
    car_part = (r.get("car") or "").strip() or lead_name.split("—")[0].strip() or "авто"
    req_part = (r.get("request") or "").strip()
    if not phone_guess:
        phone_guess = contact_phone(lid)
    info = car_part + (f" ({phone_guess})" if phone_guess else "")
    if req_part:
        info += f": {req_part}"

    if cur_status == ST_PRIEHAL:
        # Жёсткое правило: «приехал» — сделку вообще не трогаем, только заметка.
        cat = 4
        log(f"  сделка {lid} в «приехал» — только заметка")

    if cat == 1:
        # Правило 28.10 (уточнено): суть непонятна (ни о чём / обрыв / неясно что за машина) → Бадри.
        # Контур звонков целиком на Бадри (решение владельца 06.11).
        resp_id = BADRI
        if cur_status == ST_NERAZOB:
            patch["status_id"] = ST_FIRST
        task_text = (f"Kimi для Бадри: Перезвонить/актуализировать. {info}"
                     ". Далее: суть выяснилась → сделка в «Тёплые» + задача «Когда приедет?»; "
                     "3 попытки без ответа → закрыть «Не дозвонились»")
        task_due = today_18_msk()
    elif cat == 2:
        # Решение владельца 06.11: контур ЗВОНКОВ целиком на Бадри (он звонит).
        # Константин — только чат-контур Авито (chats.py), его толкает переписка.
        resp_id = BADRI
        if STAGE_RANK.get(cur_status, -1) < STAGE_RANK[ST_TEPLY]:
            patch["status_id"] = ST_TEPLY
        task_text = (f"Kimi для Бадри: Когда приедет? {info}"
                     ". Далее: назвал дату → «Приедет» + задача «Приехал?» к дате; "
                     "молчит после дожима → актуализировать, потом закрыть")
        task_due = tomorrow_10_msk()
    elif cat == 3:
        resp_id = BADRI
        if STAGE_RANK.get(cur_status, -1) < STAGE_RANK[ST_PRIEDET]:
            patch["status_id"] = ST_PRIEDET
        task_text = (f"Kimi для Бадри: Приехал? {info}"
                     ". Далее: подтвердил приезд → «Приехал»; просит перенос → новая дата "
                     "+ задача за день до приезда")
        task_due = tomorrow_10_msk()
        arr = r.get("arrival")
        now_ts = int(now.timestamp())
        if isinstance(arr, (int, float)) and now_ts - 3600 <= arr <= now_ts + 30 * 86400:
            task_due = int(arr)
    elif cat == 5:
        patch["status_id"] = ST_CLOSED
        # F12 (кейс «Авто — печка не дует» 11.11): отказ КЛИЕНТА («уже неактуально»)
        # — это «Пропала потребность», а не «Не наш профиль» (иначе сводка причин
        # закрытий врёт и причёска объявлений идёт по ложным данным).
        tail5 = " ".join(str(r.get(k) or "") for k in ("close_reason", "outcome")) \
                + "\n" + (transcript or "")[-800:]
        if CLIENT_REFUSED_RE.search(tail5):
            patch["loss_reason_id"] = LOSS_PROPALA
        else:
            patch["loss_reason_id"] = LOSS_NE_NASH_PROFIL
    elif cat == 4:
        # Категория 4 вне «приехал» → поднять сделку в «приехал» (повышение разрешено;
        # правило 28.10, уточнение владельца). Задачу не ставим, ответственного не меняем.
        if cur_status != ST_PRIEHAL:
            patch["status_id"] = ST_PRIEHAL

    if DRY_RUN:
        log(f"  DRY_RUN: категория {cat}, patch={patch}, задача={task_text!r}")
        return {"lead_id": lid, "category": cat, "name": lead_name, "dry": True}

    if cat != 4:
        if cat in (1, 2, 3):
            patch["responsible_user_id"] = resp_id
        patch["_embedded"] = {"tags": [{"name": TAG_CALL}]}
        amo("PATCH", "/api/v4/leads", [patch])
        # контакты сделки → то же короткое имя + телефон, если в контакте его нет
        sync_contacts(lid, lead_name, phone_guess)
    elif patch.get("status_id") == ST_PRIEHAL:
        # категория 4 вне «приехал»: только поднять этап, остальное не трогаем
        amo("PATCH", "/api/v4/leads", [{"id": lid, "status_id": ST_PRIEHAL}])

    # Порядок важен (кейс Audi Q5 06.11): сначала ЗАДАЧА, потом заметка.
    # Если заметка упадёт — на следующем прогоне fresh-расшифровки всё ещё
    # свежее last_razbor, разбор повторится и upsert_task доведёт задачу
    # (PATCH по тому же ключу) — операция идемпотентна. Наоборот не было:
    # заметка без задачи = задача потеряна навсегда.
    if cat in (1, 2, 3):
        upsert_task(lid, task_text, resp_id, task_due)
    elif cat == 5:
        close_my_tasks(lid)

    # заметка-разбор (всегда, включая категорию 4)
    amo("POST", f"/api/v4/leads/{lid}/notes", [
        {"note_type": "common", "params": {"text": build_note({**r, "category": cat}, now)}}
    ])

    log(f"  сделка {lid}: категория {cat}, имя «{lead_name}»")
    return {"lead_id": lid, "category": cat, "name": lead_name}


def process_missed(lead, notes, now):
    """Пропущенные без разбора → Бадри + задача + маркер. Без LLM.
    Если номер уже есть в базе с именем контакта — подписываем сделку по имени
    контакта и линкуем его (кейс 79281767967 06.11: «Пропущенный звонок +N»,
    хотя контакт известен)."""
    lid = lead["id"]
    name = lead.get("name") or ""
    if any("Пропущенный обработан" in note_text(n) for n in notes):
        return None
    phone = "".join(ch for ch in name.split("Звонок", 1)[-1] if ch.isdigit() or ch == "+")
    patch = {}
    if phone:
        digits = "".join(ch for ch in phone if ch.isdigit())
        try:
            cands = embedded(amo_get(f"/api/v4/contacts?query={digits}&limit=10"),
                             "contacts")
            named = [c for c in cands
                     if (c.get("name") or "").strip()
                     and not re.match(r"^[+\d\s()\-]+$", c["name"].strip())
                     and not c["name"].startswith(("Пропущенный", "Входящий",
                                                   "Исходящий", "СЛУЖЕБНЫЙ"))]
            if named:
                cname = named[0]["name"].strip()
                patch["name"] = f"{cname} — пропущенный ({phone})"
                amo("POST", f"/api/v4/leads/{lid}/links",
                    [{"to_entity_id": named[0]["id"],
                      "to_entity_type": "contacts"}])
                log(f"  сделка {lid}: номер известен — контакт «{cname}»")
        except Exception:
            pass
    task_text = f"Kimi для Бадри: Перезвонить/актуализировать по пропущенному: {phone or 'телефон в карточке'}"
    marker = f"Пропущенный обработан: {now.strftime('%d.%m.%Y')}"
    if DRY_RUN:
        log(f"  DRY_RUN: пропущенный {lid}, задача={task_text!r}")
        return {"lead_id": lid, "category": "missed", "dry": True}
    patch.update({"id": lid, "responsible_user_id": BADRI,
                  "_embedded": {"tags": [{"name": TAG_CALL}]}})
    if lead.get("status_id") == ST_NERAZOB:
        patch["status_id"] = ST_FIRST
    amo("PATCH", "/api/v4/leads", [patch])
    # телефон из имени пропущенного — в контакт (если там пусто)
    sync_contacts(lid, name or f"Пропущенный звонок {phone}", phone or None)
    amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {"text": marker}}])
    upsert_task(lid, task_text, BADRI, today_18_msk())
    log(f"  сделка {lid}: пропущенный → Бадри")
    return {"lead_id": lid, "category": "missed"}


# ---------------- разбор закрытых человеком задач ----------------

NEXT_STEP_SYSTEM = """Ты — оператор CRM автосервиса «Честный Гараж». Сотрудник выполнил задачу по сделке и закрыл её с комментарием. Реши, какой следующий шаг нужен. Верни СТРОГО один JSON-объект.

Правила:
- Обращение живое (клиент думает, договорились перезвонить, ждёт ответа) → "action": "task" и конкретная задача со следующим шагом.
- В результате НАЗВАН день/время визита («приедет в субботу в 9-00» и т.п.) → задача-контроль визита («Приехал?») и due_hours = часов ДО УТРА того дня (может быть больше 72 — считай честно; окно 8:30–9:00 дня визита). Этапы: визит подтверждён → статус «приедет» (142).
- Клиент уже приехал / машина на сервисе → "action": "status", "new_status": "priehal".
- Вопрос закрыт (не актуален, сделал у других, не наш профиль, дозвонились и отказался) → "action": "nothing".
- Задача не должна повторять то, что уже сделано. Текст задачи — с формулировкой следующего шага («Далее: …»). Без эмодзи, по-русски, не длиннее 250 символов.

Формат:
{"action": "task", "task_text": "Kimi для Бадри|Константина: …", "responsible": "Бадри", "due_hours": 12, "new_status": null, "why": "коротко"}"""


def decide_next_step(lead_name, status_id, task_result, last_razbor_text):
    user = (f"Сделка: «{lead_name}» (этап {status_id}).\n"
            f"Результат закрытой задачи сотрудника: «{task_result}».\n"
            f"Последний разбор: {last_razbor_text[:1500] or 'нет'}.\n\n"
            "Реши следующий шаг.")
    body = {
        "model": KIMI_MODEL,
        "temperature": float(os.environ.get("KIMI_TEMPERATURE", "1")),
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": NEXT_STEP_SYSTEM},
                     {"role": "user", "content": user}],
    }
    resp = http("POST", f"{KIMI_BASE}/v1/chat/completions", token=KIMI_KEY, body=body,
                tries=2, timeout=120)
    return json.loads(resp["choices"][0]["message"]["content"])


def process_closed_task_loops(now, skip_ids=None):
    """Задача «Kimi для …», закрытая ЧЕЛОВЕКОМ (updated_by = Бадри/Константин)
    без последующей открытой Kimi-задачи → анализ результата и создание
    НОВОЙ задачи (правило владельца 06.11: закрытые не воскрешаем, анализ →
    новая). Работает в конце прогона, после основного разбора."""
    skip_ids = skip_ids or set()
    from_ts = int((now - timedelta(days=2)).timestamp())
    day_ago = int(now.timestamp()) - 24 * 3600
    # фильтр статусов в amo у этого токена игнорируется — отбираем сами
    resp = amo_get(f"/api/v4/leads?filter[pipeline_id][]=11282498"
                   f"&filter[updated_at][from]={from_ts}&limit=200")
    wanted = {ST_FIRST, ST_TEPLY, ST_PRIEDET}
    leads = [l for l in embedded(resp, "leads")
             if l.get("status_id") in wanted and l["id"] not in skip_ids]
    handled, candidates = 0, 0
    for l in leads:
        lid = l["id"]
        try:
            tasks = embedded(amo_get(
                f"/api/v4/tasks?filter[entity_id][]={lid}"
                f"&filter[entity_type][]=leads&limit=100"), "tasks")
        except Exception:
            continue
        mine = [t for t in tasks if (t.get("text") or "").startswith("Kimi для")]
        if any(not t.get("is_completed") for t in mine):
            continue  # открытая Kimi-задача уже есть — всё в порядке
        closed = [t for t in mine if t.get("is_completed")
                  and (t.get("updated_at") or 0) >= day_ago
                  and t.get("updated_by") in (BADRI, KONSTANTIN)]
        if not closed:
            continue
        candidates += 1
        last = max(closed, key=lambda t: t.get("updated_at") or 0)
        result_txt = ""
        try:
            res = last.get("result")
            if isinstance(res, list) and res:
                result_txt = str(res[0].get("text") or "")
            elif isinstance(res, dict):
                result_txt = str(res.get("text") or "")
        except Exception:
            pass
        if not result_txt.strip():
            continue  # закрыта без комментария — не гадаем
        # F22 (кейс «Пропущенный +79283033740» 11.11): результат закрытой
        # человеком задачи читаем ДЕТЕРМИНИРОВАННО — если там «не наш профиль/
        # закрыть/не актуально/у других» — сделка закрывается сразу, без LLM
        # и без новых задач. Правило владельца 12.11: ручное закрытие финально,
        # возвращать задачи не надо. LLM-цикл (decide_next_step) — только для
        # живых исходов («перезвонил», «приедет», «думает»).
        low = result_txt.lower()
        if re.search(r"не наш профиль|не делаем|не занимаемся|не бер[её]м в работу|"
                     r"закрыть|не актуал|передумал|у других|сделал у других|"
                     r"не дозвон|не берет|не берёт|не беру[тт]", low):
            loss = (LOSS_NE_NASH_PROFIL
                    if re.search(r"профиль|не делаем|не занимаемся|не бер[её]м в работу", low)
                    else LOSS_DRUGIE if re.search(r"у других|другой сервис", low)
                    else LOSS_NE_DOZVON if re.search(r"не дозвон|не берет|не берёт|не беру[тт]", low)
                    else LOSS_PROPALA)
            if not DRY_RUN:
                amo("PATCH", "/api/v4/leads", [{"id": lid, "status_id": ST_CLOSED,
                                                "loss_reason_id": loss}])
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                    "text": f"Закрыто по результату задачи, закрытой человеком («{result_txt[:120]}») — "
                            f"детерминированное правило F22, без LLM. Ручное закрытие финально."}}])
                close_my_tasks(lid)
            log(f"  цикл задачи {lid}: результат «{result_txt[:50]}» → сделка закрыта (F22)")
            handled += 1
            continue
        try:
            notes = embedded(amo_get(f"/api/v4/leads/{lid}/notes?limit=100"), "notes")
            razbors = [(n.get("created_at") or 0, note_text(n)) for n in notes
                       if note_text(n).startswith("Разбор звонка:")
                       or note_text(n).startswith("Разбор чата Авито:")]
            last_razbor = sorted(razbors)[-1][1] if razbors else ""
            if DRY_RUN:
                log(f"  DRY_RUN цикл задачи {lid}: результат «{result_txt[:60]}»")
                handled += 1
                continue
            d = decide_next_step(l.get("name") or f"Сделка {lid}", l.get("status_id"),
                                 result_txt, last_razbor)
            action = d.get("action")
            if action == "status" and d.get("new_status") == "priehal":
                amo("PATCH", "/api/v4/leads", [{"id": lid, "status_id": ST_PRIEHAL}])
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                    "text": f"По результату закрытой задачи («{result_txt[:120]}») — клиент приехал, "
                            f"сделка поднята в «Приехал» (шаг разбора закрытых задач)."}}])
                handled += 1
            elif action == "status" and d.get("new_status") == "priedet":
                amo("PATCH", "/api/v4/leads", [{"id": lid, "status_id": ST_PRIEDET}])
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                    "text": f"По результату закрытой задачи («{result_txt[:120]}») — визит подтверждён, "
                            f"сделка переведена в «Приедет» (шаг разбора закрытых задач)."}}])
                handled += 1
            elif action == "task" and d.get("task_text"):
                resp_id = KONSTANTIN if d.get("responsible") == "Константин" else BADRI
                due_h = d.get("due_hours") if isinstance(d.get("due_hours"), (int, float)) else 12
                due = int(now.timestamp()) + max(1, min(int(due_h), 96)) * 3600
                upsert_task(lid, str(d["task_text"]).strip(), resp_id, due)
                amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                    "text": f"Задача закрыта сотрудником («{result_txt[:120]}») → создан следующий шаг "
                            f"(шаг разбора закрытых задач): {d.get('why') or ''}"}}])
                handled += 1
        except Exception as e:
            log(f"  цикл задачи {lid}: ОШИБКА {e}")
    log(f"шаг закрытых задач: кандидатов {candidates}, обработано {handled}")
    return {"candidates": candidates, "handled": handled}


# ---------------- main ----------------

def run():
    now = datetime.now(MSK)
    log(f"=== Прогон разбора звонков {'(DRY_RUN) ' if DRY_RUN else ''}{now.strftime('%d.%m.%Y %H:%M')} мск ===")
    if not AMO_TOKEN:
        raise RuntimeError("AMO_TOKEN не задан")
    if not KIMI_KEY and not DRY_RUN:
        raise RuntimeError("MOONSHOT_API_KEY не задан")

    # рабочее окно (мск): триггер тикает каждые 30 минут круглосуточно,
    # вне окна выходим молча. В DRY_RUN окно не действует (ручные тесты).
    if not DRY_RUN:
        def _hm(s):
            h, m = s.split(":")
            return int(h) * 60 + int(m)
        cur = now.hour * 60 + now.minute
        if not (_hm(os.environ.get("WINDOW_START", "9:00")) <= cur <= _hm(os.environ.get("WINDOW_END", "20:00"))):
            log("вне рабочего окна — выход без действий")
            print("SUMMARY_JSON:" + json.dumps(
                {"run": now.strftime("%d.%m.%Y %H:%M"), "window_skip": True},
                ensure_ascii=False))
            return {"run": now.strftime("%d.%m.%Y %H:%M"), "window_skip": True}

    # проверка доступа
    acc = amo_get("/api/v4/account")
    log(f"amo: {acc.get('name') or acc.get('subdomain') or 'ok'}")

    # ШАГ 0: перенос просроченных задач (правило 28.10)
    overdue = carry_overdue_tasks(now)

    # лоток «Неразобранное»: пропущенные из телефонии принимаем в воронку
    unsorted_handled = process_unsorted(now)

    from_ts = int((now - timedelta(days=3)).timestamp())
    leads, url = [], f"{AMO_BASE}/api/v4/leads?limit=100&order[updated_at]=desc&filter[updated_at][from]={from_ts}"
    while url:
        resp = http("GET", url, token=AMO_TOKEN)
        leads.extend(embedded(resp, "leads"))
        nxt = (resp.get("_links") or {}).get("next") or {}
        url = nxt.get("href")
    log(f"сделок за 3 дня: {len(leads)}")

    # ремонт: номер есть в имени сделки, но не в контакте — дозаписать (видимость дублей)
    repaired_phones = repair_phones(leads)

    processed, skipped, errors = [], [], []
    for lead in leads:
        if len(processed) >= MAX_LEADS_PER_RUN:
            log(f"лимит {MAX_LEADS_PER_RUN} сделок за прогон достигнут, остальное — на следующий прогон")
            break
        lid = lead.get("id")
        try:
            if lead.get("status_id") in (ST_SUCCESS, ST_CLOSED):
                continue
            notes = embedded(amo_get(f"/api/v4/leads/{lid}/notes?limit=250"), "notes")
            texts = [(n, note_text(n)) for n in notes]

            # 0) оболочки телефонии «Исходящий/Входящий звонок <номер>»:
            # если по номеру уже есть открытая сделка — это дубль обратного
            # звонка (кейс 79034388810 06.11: менеджер перезвонил клиенту, у
            # которого уже была сделка «Suzuki Alto — промывка печки»).
            # Свежие расшифровки переносим в основную сделку, оболочку закрываем.
            lname = (lead.get("name") or "").lower()
            if re.match(r"^(исходящий|входящий) звонок", lname):
                phone = extract_phone(lead.get("name") or "")
                dup = find_open_lead_by_phone(phone) if phone else None
                if dup and dup != lid:
                    fresh_trans = [note_text(n) for n in notes
                                   if "расшифровка" in note_text(n).lower()
                                   or "содержание звонка" in note_text(n).lower()]
                    if not DRY_RUN:
                        for ft in fresh_trans:
                            amo("POST", f"/api/v4/leads/{dup}/notes", [{"note_type": "common",
                                "params": {"text": f"Перенос расшифровки из дубля {lid} "
                                                   f"(тот же номер {phone}):\n{ft[:3000]}"}}])
                        amo("PATCH", "/api/v4/leads", [{"id": lid, "status_id": ST_CLOSED,
                                                        "loss_reason_id": LOSS_DUBL}])
                        amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                            "text": f"Дубль: по номеру {phone} уже есть открытая сделка {dup}. "
                                    f"Расшифровки перенесены в основную сделку. Закрыто автоматически."}}])
                        close_my_tasks(lid)
                        # верификация переноса: заметка должна физически появиться
                        # в основной сделке (кейс 09.11: POST прошёл без ошибки,
                        # а заметки в основной сделке не оказалось — она навсегда
                        # осталась «Пропущенный звонок +N», разбор не запустился)
                        try:
                            dup_notes = embedded(amo_get(
                                f"/api/v4/leads/{dup}/notes?limit=250"), "notes")
                            missing = [ft for ft in fresh_trans
                                       if not any(ft[:120] in note_text(n)
                                                  for n in dup_notes)]
                            if missing:
                                amo("POST", f"/api/v4/leads/{dup}/notes",
                                    [{"note_type": "common", "params": {"text":
                                        "ВНИМАНИЕ: перенос расшифровки из дубля "
                                        "заявлен, но заметка не появилась в "
                                        f"карточке. Проверить дубль {lid} вручную."}}])
                                log(f"  сделка {dup}: перенос расшифровки НЕ "
                                    f"подтверждён ({len(missing)} зам.)")
                        except Exception as e:
                            log(f"  сделка {dup}: не удалось проверить перенос ({e})")
                    log(f"  сделка {lid}: оболочка звонка {phone} — дубль {dup}, закрыто")
                    processed.append({"lead_id": lid, "category": "call-shell-dup", "dup_of": dup})
                    continue

            # 1) пропущенные / недозвоны
            if lname.startswith("пропущенный звонок") or "недозвон" in lname:
                # Правило владельца 06.11: 3 попытки дозвона без ответа →
                # закрываем «не реализовано» с причиной «Не дозвонились».
                # Разговор считаем состоявшимся при звонке длиннее 20 сек.
                attempts = [n for n in notes if n.get("note_type") == "call_out"]
                talked = any(((n.get("params") or {}).get("duration") or 0) > 20
                             for n in notes
                             if n.get("note_type") in ("call_in", "call_out"))
                if len(attempts) >= 3 and not talked:
                    if not DRY_RUN:
                        amo("PATCH", "/api/v4/leads", [{"id": lid, "status_id": ST_CLOSED,
                                                        "loss_reason_id": LOSS_NE_DOZVON}])
                        amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                            "text": f"Закрыто автоматически: {len(attempts)} попыток дозвона, "
                                    f"абонент не отвечает (правило владельца 06.11)."}}])
                        close_my_tasks(lid)
                    log(f"  сделка {lid}: {len(attempts)} попыток без ответа → закрыто «не дозвонились»")
                    processed.append({"lead_id": lid, "category": "no-answer-close"})
                    continue
                res = process_missed(lead, notes, now)
                if res:
                    processed.append(res)
                    continue
                # маркер «Пропущенный обработан» уже стоит — повторно задачу
                # не ставим, НО свежие расшифровки разговоров разбираем ниже
                # наравне с остальными: иначе сделка навсегда застревала в этой
                # ветке и не переименовывалась/не двигалась (кейс Акцент 06.11).

            # 2) расшифровки Войси
            trans = [n for n, t in texts if "расшифровка" in t.lower()]
            razbors = [n for n, t in texts if t.startswith("Разбор звонка:")]
            last_razbor = max((n.get("created_at", 0) for n in razbors), default=0)
            fresh = [n for n in trans if n.get("created_at", 0) > last_razbor]
            if not fresh:
                continue
            # 3) свежий звонок без расшифровки — Войси не успел, переносим
            calls = [n for n in notes if n.get("note_type") in ("call_in", "call_out")]
            last_call = max((n.get("created_at", 0) for n in calls), default=0)
            last_trans = max((n.get("created_at", 0) for n in trans), default=0)
            if last_call > last_trans and last_call > last_razbor:
                wait_h = (now.timestamp() - last_call) / 3600.0
                log(f"  сделка {lid}: свежий звонок без расшифровки — переносим")
                # ждём Войси 6 часов, дальше — флаг менеджеру (один раз)
                if wait_h > 6 and not any("Расшифровка Войси не появилась" in t for _, t in texts):
                    amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {
                        "text": f"Расшифровка Войси не появилась за {int(wait_h)} ч после звонка. "
                                f"Проверьте запись вручную: разбор автоматикой невозможен без текста."}}])
                    log(f"  сделка {lid}: флаг менеджеру — расшифровки нет {int(wait_h)} ч")
                skipped.append({"lead_id": lid, "reason": "нет расшифровки Войси"})
                continue

            fresh_sorted = sorted(fresh, key=lambda n: n.get("created_at", 0))
            parts = []
            for i, n in enumerate(fresh_sorted, 1):
                txt = clean_text(note_text(n))
                if txt:
                    parts.append(f"=== ЗВОНОК {i} ===\n{txt}")
            transcript = "\n\n".join(parts)
            if len(transcript) < 30:
                log(f"  сделка {lid}: расшифровка слишком короткая — пропуск")
                skipped.append({"lead_id": lid, "reason": "короткая расшифровка"})
                continue

            if DRY_RUN and not KIMI_KEY:
                # тест без ИИ: заглушка, проверяем только пайплайн amo
                r = {"category": 1, "lead_name": "", "car": "?", "request": "?",
                     "outcome": "DRY_RUN без LLM", "quality": None, "action": None,
                     "arrival": None, "close_reason": None}
            else:
                r = classify(transcript, lead.get("name") or "")
            phone_guess = extract_phone(transcript) or extract_phone(lead.get("name") or "")
            processed.append(apply_result(lead, r, now, phone_guess, transcript))
        except Exception as e:
            log(f"  сделка {lid}: ОШИБКА {e}")
            errors.append({"lead_id": lid, "error": str(e)[:300]})

    # Правило владельца 06.11: закрытая человеком задача НЕ воскрешается —
    # её результат анализируется и создаётся НОВАЯ задача. Кейс Audi Q5:
    # разбор обновил ещё открытую старую задачу, Бадри её закрыл — и больше
    # ничто не ставило следующий шаг.
    closed_loop = process_closed_task_loops(now, skip_ids={p.get("lead_id") for p in processed})

    summary = {
        "run": now.strftime("%d.%m.%Y %H:%M"),
        "dry_run": DRY_RUN,
        "overdue": overdue,
        "unsorted_missed": unsorted_handled,
        "phones_repaired": repaired_phones,
        "processed": len(processed),
        "skipped": len(skipped),
        "errors": len(errors),
        "closed_task_loops": closed_loop,
        "details": processed,
        "skipped_details": skipped,
        "error_details": errors,
    }
    log(f"=== Итог: разобрано {len(processed)}, отложено {len(skipped)}, ошибок {len(errors)}, "
        f"закрытые задачи → новый шаг: {closed_loop.get('handled', 0)} ===")
    return summary


def handler(event, context):
    try:
        summary = run()
        return {"statusCode": 200, "body": json.dumps(summary, ensure_ascii=False)}
    except Exception as e:
        log(f"ФАТАЛЬНАЯ ОШИБКА: {e}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)}, ensure_ascii=False)}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
