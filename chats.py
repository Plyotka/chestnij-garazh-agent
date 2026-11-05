# -*- coding: utf-8 -*-
"""
Чат-контур «Честный Гараж» — агент вступает ПОСЛЕ диалога Бадри.
Работает после razbor.py в том же job GitHub Actions. Компьютер не нужен.

Логика (решение владельца 05.11):
  - Бадри отвечает первичные контакты и общается с клиентом. Пока Бадри не ответил —
    агент чат НЕ трогает (никаких черновиков).
  - Когда в чате есть ответы сервиса (Бадри пообщался), агент читает переписку
    и определяет: есть интерес или нет.
  - Интерес есть → агент вступает: принимает заявку в воронку, называет сделку,
    двигает этап, ставит задачи, и САМ пишет клиенту в чат Авито строго по шаблону
    (уточнить время приезда / запросить телефон). Телефон из чата — в контакт.
  - Интереса нет → принять и закрыть 143 с причиной (причина уходит в сводку
    для причёски объявлений).

ЖЁСТКИЕ ОГРАНИЧЕНИЯ:
  - Агент не пишет клиенту ничего, кроме утверждённых шаблонов TEMPLATES ниже.
  - Если телефон клиент уже оставил — агент НЕ пишет, только двигает CRM.
  - Чаты без ответа сервиса пропускаются молча (их ведёт Бадри).
  - Только 9:00–20:00 мск (рабочее окно проверяется тут же; razbor.py дублирует).

Переменные окружения:
  AVITO_CLIENT_ID / AVITO_CLIENT_SECRET — приложение Авито (GitHub Secrets)
  AMO_TOKEN, MOONSHOT_API_KEY — общие с razbor.py
  CHAT_WINDOW_DAYS — глубина свежих чатов (по умолчанию 3)
  MAX_CHATS_PER_RUN — лимит заявок за прогон (по умолчанию 10)
  DRY_RUN — "true": только лог, без записи в amo и без отправки клиенту
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
AVITO_UID = 12626609

AMO_BASE = os.environ.get("AMO_BASE", "https://autoservice61.amocrm.ru").rstrip("/")
AMO_TOKEN = os.environ.get("AMO_TOKEN", "")
KIMI_BASE = os.environ.get("KIMI_BASE", "https://api.moonshot.ai").rstrip("/")
KIMI_KEY = os.environ.get("MOONSHOT_API_KEY", "")
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2.6")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
# NO_SEND=1 — CRM разбираем как обычно, но клиенту не пишем: вместо отправки
# шаблона ставим задачу человеку на 9:00 с текстом шаблона (ночные прогоны).
NO_SEND = os.environ.get("NO_SEND", "false").lower() == "true"
WINDOW_DAYS = int(os.environ.get("CHAT_WINDOW_DAYS", "3"))
MAX_PER_RUN = int(os.environ.get("MAX_CHATS_PER_RUN", "6"))

# люди и статусы (синхронно с razbor.py)
BADRI = 14367338
KONSTANTIN = 14221122
ST_FIRST = 88470414   # Первичный контакт
ST_TEPLY = 88470418   # Тёплые
ST_PRIEDET = 88470422 # приедет
ST_CLOSED = 143       # Закрыто не реализовано
LOSS_NE_NASH_PROFIL = 24763846
TAG_CHAT = "Чат"  # тег сделок чат-контура Авито
PHONE_FIELD = 1133163

# утверждённые шаблоны исходящих (только они, ничего от LLM клиенту)
T_WHEN = ("Здравствуйте! Подскажите, когда планируете подъехать? "
          "Подберём удобное время.")
T_PHONE = ("Отлично! Оставьте, пожалуйста, номер телефона — зафиксируем запись "
           "и перезвоним подтвердить.")

SYSTEM = """Ты — оператор сделок автосервиса «Честный Гараж». По переписке чата Авито определи, есть ли у клиента интерес к услуге, и верни СТРОГО один JSON-объект.

ИНТЕРЕС ЕСТЬ, если клиент обсуждает выполнение работы (уточняет цену/сроки/запчасти/приезд) и НЕ отказался. «Дорого, но не отказался», «подумаю», «приеду» — это интерес.
ИНТЕРЕСА НЕТ, если: клиент отказался; «не делаем такое» (тюнинг, чип и пр. — не наш профиль); клиент пропал после цены и явно ушёл; просьба невозможна.

ПОЛЯ:
- interest: true/false
- reason: краткая причина (для закрытия/сводки), если интереса нет; иначе null
- car: марка модель год, если ясно, иначе null
- request: суть запроса 5–10 слов
- client_has_phone: true, если клиент сам оставлял телефон в чате; иначе false
- phone: номер телефона из чата (формат 89xxxxxxxxx или +7…), если есть; иначе null
- said_will_come: true, если клиент сказал, что приедет (даже без даты); иначе false
- next_step: одно из "when" (интерес есть, время приезда неясно → уточнить когда приедет), "phone" (готов приехать → запросить телефон), "none" (писать не надо)
- summary: одна фраза итога для внутренней заметки

Формат: {"interest": true, "reason": null, "car": "…", "request": "…", "client_has_phone": false, "phone": null, "said_will_come": false, "next_step": "when", "summary": "…"}"""


def log(msg):
    print(f"[{datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')}] {msg}", flush=True)


def http(method, url, token=None, body=None, tries=3, timeout=45, form=False):
    if form:
        data = body.encode("utf-8") if isinstance(body, str) else None
    else:
        data = json.dumps(body).encode("utf-8") if body is not None else None
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        if form:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        else:
            req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "chg-chats/1.0")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            try:
                last = f"HTTP {e.code}: {e.read().decode('utf-8')[:200]}"
            except Exception:
                last = f"HTTP {e.code}"
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {last}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = str(e)
            if i < tries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise RuntimeError(f"{method} {url} -> {last}")


def avito_token():
    r = http("POST", "https://api.avito.ru/token", form=True, body=(
        f"grant_type=client_credentials"
        f"&client_id={os.environ['AVITO_CLIENT_ID']}"
        f"&client_secret={os.environ['AVITO_CLIENT_SECRET']}"))
    return r["access_token"]


def amo_get(path):
    return http("GET", f"{AMO_BASE}{path}", token=AMO_TOKEN)


def amo(method, path, body):
    return http(method, f"{AMO_BASE}{path}", token=AMO_TOKEN, body=body)


def embedded(resp, key):
    return (resp.get("_embedded") or {}).get(key) or []


def msg_text(m):
    return ((m.get("content") or {}).get("text") or f"[{m.get('type') or 'файл'}]").strip()


def classify(transcript, client_name, item_title):
    user = (f"Объявление: «{item_title}»\nКлиент: {client_name}\n\nПЕРЕПИСКА:\n"
            + transcript[:6000])
    msgs = [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]
    last_err = None
    for attempt in range(3):
        body = {
            "model": KIMI_MODEL,
            "temperature": float(os.environ.get("KIMI_TEMPERATURE", "1")),
            "response_format": {"type": "json_object"},
            "messages": msgs,
        }
        try:
            resp = http("POST", f"{KIMI_BASE}/v1/chat/completions", token=KIMI_KEY,
                        body=body, tries=2, timeout=120)
            content = (resp["choices"][0]["message"]["content"] or "").strip()
            content = re.sub(r"^```(json)?|```$", "", content, flags=re.MULTILINE).strip()
            return json.loads(content)
        except (json.JSONDecodeError, KeyError, RuntimeError) as e:
            last_err = e
            log(f"    classify: попытка {attempt + 1} не удалась ({e})")
            msgs = msgs + [{"role": "user", "content":
                            "Верни СТРОГО один JSON-объект, без текста до и после."}]
            time.sleep(3)
    raise RuntimeError(f"LLM не вернул JSON: {last_err}")


def today_18_msk():
    d = datetime.now(MSK).date()
    return int(datetime(d.year, d.month, d.day, 18, 0, tzinfo=MSK).timestamp())


def tomorrow_10_msk():
    d = datetime.now(MSK).date() + timedelta(days=1)
    return int(datetime(d.year, d.month, d.day, 10, 0, tzinfo=MSK).timestamp())


def next_9_msk():
    """Ближайшие 9:00 мск: сегодня, если ещё не наступили, иначе завтра."""
    now = datetime.now(MSK)
    d = now.date()
    ts = datetime(d.year, d.month, d.day, 9, 0, tzinfo=MSK)
    if now >= ts:
        ts += timedelta(days=1)
    return int(ts.timestamp())


def norm_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    return None


def find_chat_for_item(item, by_name):
    md = item.get("metadata") or {}
    name = (md.get("from") or "").strip().lower()
    got = md.get("received_at") or 0
    last = md.get("last_message_text") or 0
    best, best_d = None, 1e18
    for c in by_name.get(name, []):
        d = abs((c.get("created") or 0) - got)
        d = min(d, abs((c.get("updated") or 0) - last))
        if d < best_d:
            best, best_d = c, d
    return (best, best_d) if best_d <= 120 else (None, best_d)


def run():
    now = datetime.now(MSK)
    log(f"=== Чат-контур v2 {'(DRY_RUN) ' if DRY_RUN else ''}{now.strftime('%d.%m.%Y %H:%M')} мск ===")
    if not AMO_TOKEN:
        raise RuntimeError("AMO_TOKEN не задан")
    if not KIMI_KEY:
        raise RuntimeError("MOONSHOT_API_KEY не задан")

    # рабочее окно (мск): клиентам не пишем ночью, поэтому вне окна
    # весь чат-контур молчит (как razbor.py). В DRY_RUN окно не действует.
    if not DRY_RUN:
        def _hm(s):
            h, m = s.split(":")
            return int(h) * 60 + int(m)
        cur = now.hour * 60 + now.minute
        if not (_hm(os.environ.get("WINDOW_START", "9:00")) <= cur <= _hm(os.environ.get("WINDOW_END", "20:00"))):
            log("вне рабочего окна — выход без действий")
            return {"run": now.strftime("%d.%m.%Y %H:%M"), "window_skip": True}

    tok = avito_token()
    acc = amo_get("/api/v4/account")
    log(f"amo: {acc.get('name') or acc.get('subdomain') or 'ok'}; avito: токен получен")

    from_ts = int((now - timedelta(days=WINDOW_DAYS)).timestamp())
    chats = http("GET",
                 f"https://api.avito.ru/messenger/v2/accounts/{AVITO_UID}/chats?limit=100",
                 token=tok).get("chats") or []
    fresh = [c for c in chats if (c.get("updated") or 0) >= from_ts]
    log(f"чатов в выдаче: {len(chats)}, свежих за {WINDOW_DAYS} дн: {len(fresh)}")

    messages = {}
    for c in fresh:
        cid = c["id"]
        try:
            m = http("GET",
                     f"https://api.avito.ru/messenger/v3/accounts/{AVITO_UID}/chats/{cid}/messages/",
                     token=tok)
            messages[cid] = m.get("messages", [])
        except Exception as e:
            log(f"  чат {cid[:16]}…: не прочитан ({e})")
        time.sleep(0.1)

    by_name = {}
    for c in fresh:
        nm = ((c.get("users") or [{}])[0].get("name") or "").strip().lower()
        by_name.setdefault(nm, []).append(c)

    unsorted = [u for u in embedded(amo_get("/api/v4/leads/unsorted?limit=200"), "unsorted")
                if "avito" in (u.get("source_name") or "").lower()]
    log(f"чат-заявок в «Неразобранном»: {len(unsorted)} (беру до {MAX_PER_RUN})")

    # FIFO: берём САМЫЕ СТАРЫЕ заявки первыми — иначе хвост (старые чаты)
    # голодал: свежие падали сверху списка и съедали лимит каждый прогон.
    unsorted.sort(key=lambda u: u.get("created_at") or 0)

    # антидубль внутри прогона: один клиент = один avito user id.
    # Клиент может написать с НЕСКОЛЬКИХ объявлений — придёт несколько заявок
    # в лоток; без этой проверки контур создаст по сделке на каждое объявление
    # (кейс Юлианны 05.11: две сделки «Шиномонтаж» с разницей в минуту).
    def chat_uid(c):
        try:
            return c["users"][0].get("id") or None
        except (KeyError, IndexError, TypeError):
            return None

    seen_uid = {}  # avito user id -> id созданной сделки

    acted, skipped, errors = [], [], []
    for item in unsorted[:MAX_PER_RUN]:
        uid = item.get("uid")
        md = item.get("metadata") or {}
        cname = md.get("from") or "?"
        try:
            chat, delta = find_chat_for_item(item, by_name)
            if not chat:
                skipped.append({"uid": uid, "reason": f"чат не найден (Δ{int(delta)}с)"})
                continue
            cid = chat["id"]
            msgs = messages.get(cid) or []
            outs = [m for m in msgs if m.get("direction") == "out"]
            if not outs:
                skipped.append({"uid": uid, "reason": "Бадри ещё не отвечал — агент молчит"})
                log(f"  {cname}: Бадри ещё не отвечал — пропуск")
                continue

            # антидубль: тот же клиент (avito user id) уже обработан в этом прогоне
            au = chat_uid(chat)
            if au and au in seen_uid:
                if DRY_RUN:
                    log(f"  DRY_RUN {cname}: дубль клиента (уже сделка {seen_uid[au]})")
                    skipped.append({"uid": uid, "reason": f"дубль клиента, сделка {seen_uid[au]}"})
                    continue
                acc = amo("POST", f"/api/v4/leads/unsorted/{uid}/accept",
                          {"user_id": BADRI, "status_id": ST_FIRST})
                dup_id = ((acc.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
                if dup_id:
                    amo("PATCH", "/api/v4/leads", [{"id": dup_id, "status_id": ST_CLOSED,
                                                    "loss_reason_id": 24765102}])  # Дубль
                    amo("POST", f"/api/v4/leads/{dup_id}/notes", [{"note_type": "common", "params": {
                        "text": f"Дубль: клиент {cname} писал с нескольких объявлений. "
                                f"Основная сделка: {seen_uid[au]}. Закрыто автоматически чат-контуром."}}])
                log(f"  {cname}: дубль клиента — принят и закрыт (основная сделка {seen_uid[au]})")
                acted.append({"lead": dup_id, "client": cname, "interest": None, "dup_of": seen_uid[au]})
                continue

            parts = []
            for m in msgs:
                who = "КЛИЕНТ" if m.get("direction") == "in" else "СЕРВИС"
                t = msg_text(m)
                if t:
                    parts.append(f"{who}: {t}")
            transcript = "\n".join(parts)
            if len(transcript) < 10:
                skipped.append({"uid": uid, "reason": "пустая переписка"})
                continue

            item_title = (((chat.get("context") or {}).get("value") or {}).get("title")) or "услуга сервиса"
            r = classify(transcript, cname, item_title)
            interest = bool(r.get("interest"))
            request = (r.get("request") or "").strip()
            lead_name = f"{cname} — {request[:60]}" if request else f"Авито — {cname}"
            phone = norm_phone(r.get("phone") or "")

            if DRY_RUN:
                log(f"  DRY_RUN {cname}: interest={interest}, next={r.get('next_step')}, phone={phone or '—'}")
                acted.append({"client": cname, "interest": interest, "dry": True})
                continue

            # принимаем заявку
            acc = amo("POST", f"/api/v4/leads/unsorted/{uid}/accept",
                      {"user_id": BADRI, "status_id": ST_FIRST})
            new_id = ((acc.get("_embedded") or {}).get("leads") or [{}])[0].get("id")
            if not new_id:
                raise RuntimeError("accept не вернул id сделки")

            note = "\n".join([
                f"Разбор чата Авито: {now.strftime('%d.%m.%Y %H:%M')}",
                f"Объявление: {item_title}",
                f"Клиент: {cname}",
                f"Авто: {r.get('car') or '—'}",
                f"Запрос: {request or '—'}",
                f"Итог: {'интерес' if interest else 'нет интереса'} — {r.get('summary') or ''}",
            ])

            patch = {"id": new_id, "name": lead_name,
                     "_embedded": {"tags": [{"name": TAG_CHAT}]}}
            tasks = []
            if not interest:
                patch["status_id"] = ST_CLOSED
                patch["loss_reason_id"] = LOSS_NE_NASH_PROFIL
                note += f"\nПричина закрытия: {r.get('reason') or '—'}"
            else:
                if r.get("said_will_come") or phone:
                    patch["status_id"] = ST_PRIEDET
                    patch["responsible_user_id"] = KONSTANTIN
                    tasks.append(("Kimi для Константина: Приехал?", KONSTANTIN, tomorrow_10_msk()))
                else:
                    patch["status_id"] = ST_TEPLY
                    patch["responsible_user_id"] = KONSTANTIN
                    tasks.append(("Kimi для Константина: Когда приедет?", KONSTANTIN, tomorrow_10_msk()))

            amo("PATCH", "/api/v4/leads", [patch])

            # телефон из чата — в контакт
            if phone:
                contact_name = lead_name
                created = amo("POST", "/api/v4/contacts", [{
                    "name": contact_name,
                    "custom_fields_values": [{"field_id": PHONE_FIELD, "values": [{"value": phone}]}],
                    "_embedded": {"leads": [{"id": new_id}]}}])
                note += f"\nТелефон из чата: {phone}"
            elif not interest:
                pass

            # агент сам пишет клиенту — строго по шаблону и только если телефона ещё нет.
            # В NO_SEND клиенту не пишем (ночь): вместо отправки — задача Бадри на 9:00 с текстом шаблона.
            sent = None
            if interest and not phone:
                tpl = None
                if r.get("next_step") == "phone" or r.get("said_will_come"):
                    tpl = T_PHONE
                elif r.get("next_step") == "when":
                    tpl = T_WHEN
                if tpl:
                    if NO_SEND:
                        note += f"\nШаблон НЕ отправлен (ночной прогон NO_SEND): «{tpl}»"
                        tasks.append((f"Kimi для Бадри: в чат Авито отправить клиенту: «{tpl}»",
                                      BADRI, next_9_msk()))
                    else:
                        http("POST",
                             f"https://api.avito.ru/messenger/v1/accounts/{AVITO_UID}/chats/{cid}/messages",
                             token=tok, body={"message": {"text": tpl}, "type": "text"})
                        sent = tpl
                        note += f"\nОтправлено клиенту: «{tpl}»"

            amo("POST", f"/api/v4/leads/{new_id}/notes", [
                {"note_type": "common", "params": {"text": note}}])
            if tasks:
                amo("POST", "/api/v4/tasks", [{
                    "text": t[0], "responsible_user_id": t[1],
                    "complete_till": t[2], "entity_id": new_id, "entity_type": "leads"} for t in tasks])

            acted.append({"lead": new_id, "client": cname, "interest": interest,
                          "sent": bool(sent), "no_send": bool(NO_SEND and interest and not phone and tpl)})
            if au:
                seen_uid[au] = new_id
            log(f"  {cname}: сделка {new_id}, interest={interest}" + (", написали клиенту" if sent else ""))
        except Exception as e:
            errors.append({"uid": uid, "error": str(e)[:250]})
            log(f"  ОШИБКА {cname}: {e}")

    summary = {"run": now.strftime("%d.%m.%Y %H:%M"), "dry_run": DRY_RUN,
               "fresh_chats": len(fresh), "unsorted_chats": len(unsorted),
               "acted": len(acted), "skipped": len(skipped), "errors": len(errors),
               "details": acted, "skipped_details": skipped, "error_details": errors}
    log(f"=== Итог: обработано {len(acted)}, пропущено {len(skipped)}, ошибок {len(errors)} ===")
    print("SUMMARY_JSON:" + json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    run()
