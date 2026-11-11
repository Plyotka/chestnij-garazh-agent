# -*- coding: utf-8 -*-
"""
Журнал прогонов: читает summary razbor.py и chats.py, пишет краткую заметку
на служебную карточку «Kimi — журнал прогонов» в amoCRM.

Вход: файлы razbor.out и chats.out (stdout обоих скриптов, tee в workflow).
Вызывается всегда (if: always()), чтобы падения контуров тоже попадали в журнал.

Переменные окружения: AMO_TOKEN, JOURNAL_LEAD_ID (id карточки-журнала).
JOURNAL_DRY=true — только печать заметки, без записи в amo.
"""
import json
import os
import re
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
AMO_BASE = "https://autoservice61.amocrm.ru"
JOURNAL_LEAD_ID = int(os.environ.get("JOURNAL_LEAD_ID", "0"))
DRY = os.environ.get("JOURNAL_DRY", "false").lower() == "true"

# P2 (11.11): радар застрявших сделок — открытые «Первичный/Тёплые/Приедет»
# без движения 3+ суток. Детерминированно, без LLM. Идея из РАЗБОР-ЗАТЫКОВ-10.11
# (сквозная причина №4: застрявшие находит только глазами владелец).
ST_OPEN = {88470414: "Первичный", 88470418: "Тёплые", 88470422: "Приедет"}
STUCK_DAYS = 3


def stuck_block(token):
    """Сводка «застрявших» сделок: без изменений STUCK_DAYS+ суток.
    Используем filter[updated_at][to] — amo отдаст ТОЛЬКО stale-сделки,
    страниц будет немного. Фильтр статусов у токена игнорируется (известная
    ловушка) — отбираем статусы сами."""
    try:
        from_ts = int(time.time()) - STUCK_DAYS * 86400
        req = urllib.request.Request(
            f"{AMO_BASE}/api/v4/leads?filter[pipeline_id][]=11282498"
            f"&filter[updated_at][to]={from_ts}&limit=250",
            headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            leads = (json.loads(r.read().decode()).get("_embedded") or {}).get("leads") or []
        rows = []
        for l in leads:
            s = l.get("status_id")
            if s in ST_OPEN:
                rows.append((l.get("updated_at") or 0, l.get("id"),
                             (l.get("name") or "").strip(), ST_OPEN[s]))
        if not rows:
            return None
        rows.sort()
        lines = [f"[РАДАР] застрявшие (без движения {STUCK_DAYS}+ суток): {len(rows)}"]
        for ts, lid, nm, st in rows[:10]:
            d = datetime.fromtimestamp(ts, MSK).strftime("%d.%m")
            lines.append(f"  • {lid} «{nm[:45]}» ({st}, с {d})")
        if len(rows) > 10:
            lines.append(f"  • … и ещё {len(rows) - 10}")
        return lines
    except Exception as e:
        return [f"[РАДАР] не удалось построить ({e})"]


RESP = {14367338: "Бадри", 14221122: "Константин"}


def queue_digest(token):
    """Утренняя сводка очереди (решение владельца 11.11): один читаемый
    список «на сегодня» вместо столбца красного. Все открытые задачи с
    сроком до конца сегодня (включая просроченные ранее), сгруппированы
    по ответственным, с названиями сделок. Детерминированно, без LLM."""
    try:
        now = datetime.now(MSK)
        end_of_day = int(datetime(now.year, now.month, now.day,
                                  23, 59, tzinfo=MSK).timestamp())
        url = (f"{AMO_BASE}/api/v4/tasks?filter[is_completed]=0"
               f"&filter[complete_till][to]={end_of_day}&limit=250")
        rows = []
        while url:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode())
            rows.extend((resp.get("_embedded") or {}).get("tasks") or [])
            url = ((resp.get("_links") or {}).get("next") or {}).get("href")
        if not rows:
            return ["[ОЧЕРЕДЬ] на сегодня: задач нет — всё чисто"]
        lead_ids = sorted({t["entity_id"] for t in rows if t.get("entity_id")})
        leads = {}
        for i in range(0, len(lead_ids), 50):
            qs = "&".join(f"filter[id][]={x}" for x in lead_ids[i:i + 50])
            req = urllib.request.Request(
                f"{AMO_BASE}/api/v4/leads?{qs}&limit=250",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode())
            for l in (resp.get("_embedded") or {}).get("leads") or []:
                leads[l["id"]] = (l.get("name") or "").strip()
        grouped = {}
        for t in rows:
            who = RESP.get(t.get("responsible_user_id"),
                           f"пользователь {t.get('responsible_user_id')}")
            eid = t.get("entity_id")
            nm = leads.get(eid) if eid else None
            label = f"«{nm[:42]}»" if nm else "(вне сделок)"
            txt = " ".join((t.get("text") or "").split())
            txt = re.sub(r"^Kimi для \S+:\s*", "", txt)
            due = datetime.fromtimestamp(t.get("complete_till") or 0, MSK)
            over = (t.get("complete_till") or 0) < int(now.timestamp())
            mark = "!" if over else " "
            grouped.setdefault(who, []).append(
                (t.get("complete_till") or 0,
                 f"  [{mark}] {due.strftime('%d.%m %H:%M')} {label} — {txt[:70]}"))
        lines = [f"[ОЧЕРЕДЬ] на сегодня: {len(rows)} задач"]
        for who in ("Бадри", "Константин"):
            if who in grouped:
                lines.append(f" {who} ({len(grouped[who])}):")
                for _, s in sorted(grouped[who]):
                    lines.append(s)
        for who, items in grouped.items():
            if who not in ("Бадри", "Константин"):
                lines.append(f" {who} ({len(items)}):")
                for _, s in sorted(items):
                    lines.append(s)
        lines.append("  [!] = просрочена (ШАГ 0 перенесёт после 19:00)")
        return lines
    except Exception as e:
        return [f"[ОЧЕРЕДЬ] не удалось построить ({e})"]


def extract_summary(path, prefix=None):
    """Последняя JSON-строка summary из stdout-файла (в логе GH перед
    JSON может стоять временная метка — ищем '{' и парсим до конца строки)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if prefix and line.startswith(prefix):
            line = line[len(prefix):].strip()
        i = line.find("{")
        if i < 0 or '"run"' not in line:
            continue
        try:
            return json.loads(line[i:])
        except json.JSONDecodeError:
            continue
    return None


def fmt_call(d):
    cat = d.get("category")
    catname = {1: "неясно→Бадри", 2: "Тёплые→Бадри", 3: "приедет→Бадри",
               4: "приехал", 5: "закрыт", "missed": "пропущенный→Бадри"}.get(cat, f"кат.{cat}")
    if d.get("dry"):
        return f"DRY {d.get('lead_id')}"
    nm = (d.get("name") or "").strip()
    return f"{d.get('lead_id')} «{nm[:45]}» → {catname}"


def fmt_chat(d):
    if d.get("dup_of"):
        return f"{d.get('client')} → дубль сделки {d['dup_of']} (закрыт)"
    if d.get("dry"):
        return f"DRY {d.get('client')}"
    if d.get("interest") is False:
        return f"{d.get('client')} → сделка {d.get('lead')}, закрыт (нет интереса)"
    if d.get("sent"):
        sent = "написали клиенту"
    elif d.get("pending"):
        sent = "шаблон → дневной дожим"
    else:
        sent = ""
    base = f"{d.get('client')} → сделка {d.get('lead')}, интерес"
    return f"{base} · {sent}" if sent else base


def bullets(items, limit=8):
    out = [f"  • {x}" for x in items[:limit]]
    if len(items) > limit:
        out.append(f"  • … и ещё {len(items) - limit}")
    return out


def main():
    now = datetime.now(MSK)
    rz = extract_summary("razbor.out")
    ch = extract_summary("chats.out", prefix="SUMMARY_JSON:")

    # токен нужен и для радара, и для записи журнала — читаем заранее
    token = None
    if not DRY and JOURNAL_LEAD_ID:
        token = os.environ.get("AMO_TOKEN") or None

    parts = [f"Прогон {now.strftime('%d.%m.%Y %H:%M')} мск"]

    if rz is None:
        parts.append("[ЗВОНКИ] прогон не отработал (см. лог GitHub)")
    elif rz.get("window_skip"):
        parts.append("[ЗВОНКИ] вне рабочего окна — прогон пропущен")
    else:
        ov = rz.get("overdue") or {}
        if ov:
            parts.append(f"  [!] просроченные задачи: перенесено {ov.get('moved', 0)}, закрыто {ov.get('closed', 0)}")
        if rz.get("unsorted_missed"):
            parts.append(f"  [лоток] обработано пропущенных из лотка: {rz['unsorted_missed']}")
        head = f"[ЗВОНКИ] разобрано {rz['processed']}, отложено {rz['skipped']}, ошибок {rz['errors']}"
        parts.append(head)
        parts += bullets([fmt_call(d) for d in rz.get("details") or []])
        warns = [f"сделка {d['lead_id']}: {d.get('reason', '')[:60]}" for d in (rz.get("skipped_details") or [])]
        errs = [f"сделка {d['lead_id']}: {d.get('error', '')[:80]}" for d in (rz.get("error_details") or [])]
        if errs:
            parts += ["  [ОШИБКИ]:"] + bullets(errs)
        elif warns:
            parts += ["  [пропуски]:"] + bullets(warns[:4])

    if ch is None:
        parts.append("[ЧАТЫ] прогон не отработал (см. лог GitHub)")
    else:
        parts.append(f"[ЧАТЫ] обработано {ch['acted']}, пропущено {ch['skipped']}, ошибок {ch['errors']} "
                     f"(заявок в лотке {ch['unsorted_chats']}, дожим отправлено {ch.get('followups', 0)})")
        parts += bullets([fmt_chat(d) for d in ch.get("details") or []])
        sk = [d.get("reason", "")[:70] for d in (ch.get("skipped_details") or [])]
        er = [d.get("error", "")[:80] for d in (ch.get("error_details") or [])]
        if er:
            parts += ["  [ОШИБКИ]:"] + bullets(er)
        elif sk:
            parts += ["  [пропуски]:"] + bullets(sk[:4])

    # P2: радар застрявших — добавляем и в дневные, и в ночные записи
    # (чтение дешёвое, а ночью карточки как раз «дозревают» без движения)
    if token:
        radar = stuck_block(token)
        if radar:
            parts += radar
        # Утренняя сводка очереди — один раз, в прогоне 9:00–10:00 мск
        # (решение владельца 11.11: один читаемый список «на сегодня»
        # вместо столбца красных просроченных задач)
        if 9 <= now.hour < 10:
            parts += queue_digest(token)

    note = "\n".join(parts)
    print(note)

    if DRY or not JOURNAL_LEAD_ID or not token:
        return
    req = urllib.request.Request(
        f"{AMO_BASE}/api/v4/leads/{JOURNAL_LEAD_ID}/notes",
        data=json.dumps([{"note_type": "common", "params": {"text": note}}]).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    print(f"[журнал] заметка добавлена на карточку {JOURNAL_LEAD_ID}")


if __name__ == "__main__":
    main()
