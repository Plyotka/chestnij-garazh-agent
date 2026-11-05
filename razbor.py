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
TAG_CALL = "Звонок"  # тег сделок звонкового контура

STAGE_RANK = {ST_NERAZOB: 0, ST_FIRST: 1, ST_TEPLY: 2, ST_PRIEDET: 3, ST_PRIEHAL: 4}


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
3 — ясно ЧТО нужно И когда приедет: договорились о дате/времени приезда.
4 — клиент УЖЕ приехал / авто на сервисе / работа в процессе.
5 — ОТКАЗ: сервис отказал (не делаем такие работы / такое авто / нет запчастей и клиент уходит).

ПРАВИЛА:
- ПРАВИЛА:
- Сомнение между категориями → выбирай меньшую (1 или 2).
- Если расшифровок несколько — это НЕСКОЛЬКО звонков по одной сделке: учитывай ВСЕ вместе. Более поздний короткий или пустой звонок НЕ отменяет информацию более раннего содержательного: имя, категорию, этап и задачу определяй по совокупности, при конфликте — по более содержательному звонку.
- Имя сделки lead_name: «<Марка Модель> — <суть не более 6 слов>», например «Renault Duster — замена глушителя». Марка неизвестна → «Авто — <суть>». ВАЖНО: если в запросе передано ТЕКУЩЕЕ ИМЯ СДЕЛКИ и оно уже содержательное (не «Входящий звонок…», не «Пропущенный…», не «MAX — …», не пустое) — верни его БЕЗ ИЗМЕНЕНИЙ, даже если расшифровка новая. Новое имя придумывай только если текущее пустое/системное, либо если звонок полностью меняет суть обращения.
- arrival: только для категории 3 — unixtime времени приезда по Europe/Moscow, если сказано в звонке, иначе null.
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
    чтобы ночные прогоны не ставили уже просроченные задачи."""
    now = datetime.now(MSK)
    d = now.date()
    ts = datetime(d.year, d.month, d.day, 18, 0, tzinfo=MSK)
    if now > ts:
        ts += timedelta(days=1)
        ts = ts.replace(hour=10, minute=0)
    return int(ts.timestamp())


def tomorrow_10_msk():
    d = datetime.now(MSK).date() + timedelta(days=1)
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
    """ШАГ 0 (правило 28.10, выполняется первым в прогоне): открытые просроченные задачи.
    Сделка в 142/143 → закрыть задачу; иначе → complete_till сегодня 18:00 мск,
    а если оно уже прошло — завтра 10:00. Текст, ответственного и связь с сущностью не менять."""
    now_ts = int(now.timestamp())
    due = today_18_msk()
    if due <= now_ts:
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
            else:
                patch_move.append({"id": t["id"], "complete_till": due})
        if DRY_RUN:
            log(f"  DRY_RUN шаг 0: перенести {len(patch_move)}, закрыть {len(patch_close)}")
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
    """Открытая сделка с таким номером (для дедупликации пропущенных)."""
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    resp = amo_get(f"/api/v4/leads?query={digits}&limit=20")
    for l in embedded(resp, "leads"):
        if l.get("status_id") in (ST_SUCCESS, ST_CLOSED):
            continue
        if digits in re.sub(r"\D", "", l.get("name") or ""):
            return l["id"]
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
                    # повторный звонок с того же номера — дубль не плодим;
                    # вместо этого вешаем задачу перезвона на открытую сделку.
                    # Заявку в лотке API отклонить не даёт (405) — остаётся для ручной чистки.
                    if not DRY_RUN:
                        amo("POST", f"/api/v4/leads/{dup}/notes", [{"note_type": "common", "params": {
                            "text": f"Повторный пропущенный звонок {phone} ({now.strftime('%d.%m.%Y %H:%M')}) — дубль не создан"}}])
                        upsert_task(dup, f"Kimi для Бадри: Перезвонить/актуализировать по пропущенному: {phone}",
                                    BADRI, today_18_msk())
                    log(f"  лоток: пропущенный {phone} — открытая сделка {dup}, дубль не создан, задача перезвона добавлена")
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


def apply_result(lead, r, now, phone_guess=None):
    lid = lead["id"]
    cat = r.get("category")
    if cat not in (1, 2, 3, 4, 5):
        raise RuntimeError(f"плохая категория от LLM: {cat!r}")
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
        task_text = f"Kimi для Бадри: Перезвонить/актуализировать. {info}"
        task_due = today_18_msk()
    elif cat == 2:
        # Решение владельца 06.11: контур ЗВОНКОВ целиком на Бадри (он звонит).
        # Константин — только чат-контур Авито (chats.py), его толкает переписка.
        resp_id = BADRI
        if STAGE_RANK.get(cur_status, -1) < STAGE_RANK[ST_TEPLY]:
            patch["status_id"] = ST_TEPLY
        task_text = f"Kimi для Бадри: Когда приедет? {info}"
        task_due = tomorrow_10_msk()
    elif cat == 3:
        resp_id = BADRI
        if STAGE_RANK.get(cur_status, -1) < STAGE_RANK[ST_PRIEDET]:
            patch["status_id"] = ST_PRIEDET
        task_text = f"Kimi для Бадри: Приехал? {info}"
        arr = r.get("arrival")
        now_ts = int(now.timestamp())
        if isinstance(arr, (int, float)) and now_ts - 3600 <= arr <= now_ts + 30 * 86400:
            task_due = int(arr)
        else:
            task_due = tomorrow_10_msk()
    elif cat == 5:
        patch["status_id"] = ST_CLOSED
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

    # заметка-разбор (всегда, включая категорию 4)
    amo("POST", f"/api/v4/leads/{lid}/notes", [
        {"note_type": "common", "params": {"text": build_note({**r, "category": cat}, now)}}
    ])

    if cat in (1, 2, 3):
        upsert_task(lid, task_text, resp_id, task_due)
    elif cat == 5:
        close_my_tasks(lid)

    log(f"  сделка {lid}: категория {cat}, имя «{lead_name}»")
    return {"lead_id": lid, "category": cat, "name": lead_name}


def process_missed(lead, notes, now):
    """Пропущенные без разбора → Бадри + задача + маркер. Без LLM."""
    lid = lead["id"]
    name = lead.get("name") or ""
    if any("Пропущенный обработан" in note_text(n) for n in notes):
        return None
    phone = "".join(ch for ch in name.split("Звонок", 1)[-1] if ch.isdigit() or ch == "+")
    task_text = f"Kimi для Бадри: Перезвонить/актуализировать по пропущенному: {phone or 'телефон в карточке'}"
    marker = f"Пропущенный обработан: {now.strftime('%d.%m.%Y')}"
    if DRY_RUN:
        log(f"  DRY_RUN: пропущенный {lid}, задача={task_text!r}")
        return {"lead_id": lid, "category": "missed", "dry": True}
    patch = {"id": lid, "responsible_user_id": BADRI,
             "_embedded": {"tags": [{"name": TAG_CALL}]}}
    if lead.get("status_id") == ST_NERAZOB:
        patch["status_id"] = ST_FIRST
    amo("PATCH", "/api/v4/leads", [patch])
    # телефон из имени пропущенного — в контакт (если там пусто)
    sync_contacts(lid, name or f"Пропущенный звонок {phone}", phone or None)
    amo("POST", f"/api/v4/leads/{lid}/notes", [{"note_type": "common", "params": {"text": marker}}])
    upsert_task(lid, task_text, BADRI, today_18_msk())
    log(f"  сделка {lid}: пропущенный → Бадри")
    return {"lead_id": lid, "category": "missed"}


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

            # 1) пропущенные
            if (lead.get("name") or "").lower().startswith("пропущенный звонок"):
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
                txt = note_text(n)
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
            processed.append(apply_result(lead, r, now, phone_guess))
        except Exception as e:
            log(f"  сделка {lid}: ОШИБКА {e}")
            errors.append({"lead_id": lid, "error": str(e)[:300]})

    summary = {
        "run": now.strftime("%d.%m.%Y %H:%M"),
        "dry_run": DRY_RUN,
        "overdue": overdue,
        "unsorted_missed": unsorted_handled,
        "phones_repaired": repaired_phones,
        "processed": len(processed),
        "skipped": len(skipped),
        "errors": len(errors),
        "details": processed,
        "skipped_details": skipped,
        "error_details": errors,
    }
    log(f"=== Итог: разобрано {len(processed)}, отложено {len(skipped)}, ошибок {len(errors)} ===")
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
