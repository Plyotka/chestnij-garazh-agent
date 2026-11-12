# -*- coding: utf-8 -*-
"""
Разовый пакет «дожим Приедет» (12.11.2026): закрытие мёртвых грузов и
разблокировка дожима. Идемпотентен: каждый шаг проверяет состояние.
Запускается вручную workflow fix.yml, использует секреты репозитория.
После выполнения workflow и скрипт удаляются (коммит очистки).
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
AMO = "https://autoservice61.amocrm.ru"
AVITO_UID = 12626609
AMO_TOKEN = os.environ["AMO_TOKEN"]
KONST = 14221122

BYE = ("Здравствуйте! Видим, что вопрос, скорее всего, уже не актуален — "
       "закрываем обращение. Если передумаете — напишите, всегда подберём время.")


def http(method, url, body=None, form=False, token=None, tries=6):
    data = (body.encode() if isinstance(body, str)
            else json.dumps(body, ensure_ascii=False).encode("utf-8")) if body is not None else None
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/x-www-form-urlencoded" if form else "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (401, 429, 500, 502, 503) and i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise
    raise last


vtok = http("POST", "https://api.avito.ru/token", form=True, body=(
    f"grant_type=client_credentials&client_id={os.environ['AVITO_CLIENT_ID']}"
    f"&client_secret={os.environ['AVITO_CLIENT_SECRET']}"))["access_token"]


def notes_of(lid):
    return (http("GET", f"{AMO}/api/v4/leads/{lid}/notes?limit=100", token=AMO_TOKEN)
            .get("_embedded", {}).get("notes") or [])


def note_text(n):
    return (n.get("params") or {}).get("text") or ""


def chat_id_of(lid):
    for n in notes_of(lid):
        m = re.search(r"chat_id=(u2i-[\w~\-]+)", note_text(n))
        if m:
            return m.group(1)
    return None


def bye_in_chat(cid):
    msgs = http("GET", f"https://api.avito.ru/messenger/v3/accounts/{AVITO_UID}/chats/{cid}/messages/",
                token=vtok).get("messages", [])
    return any("закрываем обращение" in (((m.get("content") or {}).get("text") or "").lower())
               for m in msgs)


def send(cid, text):
    http("POST", f"https://api.avito.ru/messenger/v1/accounts/{AVITO_UID}/chats/{cid}/messages",
         body={"message": {"text": text}, "type": "text"}, token=vtok)


def open_tasks(lid):
    return (http("GET", f"{AMO}/api/v4/tasks?filter[entity_id]={lid}"
                        f"&filter[entity_type][]=leads&filter[is_completed]=0&limit=50", token=AMO_TOKEN)
            .get("_embedded", {}).get("tasks") or [])


def close_tasks(lid):
    for t in open_tasks(lid):
        http("PATCH", f"{AMO}/api/v4/tasks", [{"id": t["id"], "is_completed": True}], token=AMO_TOKEN)


def note(lid, text):
    if any(text[:40] in note_text(n) for n in notes_of(lid)):
        return
    http("POST", f"{AMO}/api/v4/leads/{lid}/notes",
         [{"note_type": "common", "params": {"text": text}}], token=AMO_TOKEN)


def task(lid, text, who, till):
    if any(text[:40] in (t.get("text") or "") for t in open_tasks(lid)):
        return
    http("POST", f"{AMO}/api/v4/tasks", [{
        "text": text, "responsible_user_id": who,
        "complete_till": int(till), "entity_id": lid, "entity_type": "leads"}], token=AMO_TOKEN)


def status(lid):
    return http("GET", f"{AMO}/api/v4/leads/{lid}", token=AMO_TOKEN).get("status_id")


def close_dead(lid, why_note):
    """Прощание (если ещё не уходило) + закрытие «Пропала потребность»."""
    if status(lid) == 143:
        return
    cid = chat_id_of(lid)
    if cid and not bye_in_chat(cid):
        send(cid, BYE)
        time.sleep(1)
    close_tasks(lid)
    http("PATCH", f"{AMO}/api/v4/leads",
         [{"id": lid, "status_id": 143, "loss_reason_id": 24393498}], token=AMO_TOKEN)
    note(lid, why_note)


now = datetime.now(MSK)
t10_13 = int(datetime(now.date() + timedelta(days=1), tzinfo=MSK).replace(hour=10).timestamp())
t10_16 = int((now + timedelta(days=4)).replace(hour=10, minute=0, second=0, microsecond=0).timestamp())
t09_13 = int(datetime(now.date() + timedelta(days=1), tzinfo=MSK).replace(hour=9, minute=30).timestamp())

# 1. Денис — 5 суток тишины после confirm
close_dead(37583383,
           "Дожата по схеме 12.11: 3 касания + ручной дожим — ответа нет, тишина 5 суток. "
           "Прощание отправлено, сделка закрыта «Пропала потребность».")
print("1. Денис: закрыт", flush=True)

# 2. Даниил — 3+ суток тишины после «переносим?»
close_dead(37636553,
           "Дожата по схеме 12.11: «переносим?» 09.11 → тишина 3+ суток, ручной дожим без ответа. "
           "Прощание отправлено, закрыто «Пропала потребность».")
print("2. Даниил: закрыт", flush=True)

# 3. whtevr — снять блокер-дубль задачи, контрольная на 13.11 09:30
close_tasks(37733193)
task(37733193,
     "Kimi для Константина: за день до приезда подтвердить запись whtevr (тормоза+суппорт Civic 8, "
     "ПЯТНИЦА 13.11 ~10:00, тел. +79860081262): «всё получается? Приедете?» "
     "Далее: приехал → «Приехал»; перенос → новая дата", KONST, t09_13)
note(37733193,
     "Разбор 12.11: ложная задача «ответить по существу» (пересозданная до фикса F17) блокировала "
     "дожим через F11 — закрыта. Контрольное подтверждение на 13.11 09:30.")
print("3. whtevr: разблокирован", flush=True)

# 4. Надежда — вчерашний no-show, «актуален?» ушло 12.11 → контрольная на 13.11 10:00
close_tasks(37638423)
task(37638423,
     "Kimi для Константина: Надежда (переобувка Getz R15) — 'актуален?' отправлено 12.11, запись "
     "11.11 10-11 сорвалась (no-show). Молчит сутки после «актуален?» → прощание и закрытие "
     "«Пропала потребность»; ответила → новая дата + «Приедет»", KONST, t10_13)
note(37638423, "Разбор 12.11: вчерашний no-show, «актуален?» ушло сегодня. Контрольная на 13.11 10:00.")
print("4. Надежда: контрольная на 13.11", flush=True)

# 5. Егор Пилипец — сегодняшний no-show
note(37698079,
     "Не приехал: запись сегодня 12.11 11:00 (дворники ВАЗ 2111), в 11:10 написали «Вы где» — "
     "ответа нет. Движок поставит задачу уточнить перенос или закрыть (ветка «не приехал»).")
print("5. Егор Пилипец: заметка «не приехал»", flush=True)
print("ВСЁ ГОТОВО", flush=True)
