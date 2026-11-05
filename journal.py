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
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
AMO_BASE = "https://autoservice61.amocrm.ru"
JOURNAL_LEAD_ID = int(os.environ.get("JOURNAL_LEAD_ID", "0"))
DRY = os.environ.get("JOURNAL_DRY", "false").lower() == "true"


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
    sent = "написали клиенту" if d.get("sent") else ("шаблон→задача Бадри" if d.get("no_send") else "")
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

    parts = [f"Прогон {now.strftime('%d.%m.%Y %H:%M')} мск"]

    if rz is None:
        parts.append("📞 Звонки: прогон не отработал (см. лог GitHub)")
    else:
        ov = rz.get("overdue") or {}
        if ov:
            parts.append(f"  ⏰ просроченные задачи: перенесено {ov.get('moved', 0)}, закрыто {ov.get('closed', 0)}")
        if rz.get("unsorted_missed"):
            parts.append(f"  📥 обработано пропущенных из лотка: {rz['unsorted_missed']}")
        head = f"📞 Звонки: разобрано {rz['processed']}, отложено {rz['skipped']}, ошибок {rz['errors']}"
        parts.append(head)
        parts += bullets([fmt_call(d) for d in rz.get("details") or []])
        warns = [f"сделка {d['lead_id']}: {d.get('reason', '')[:60]}" for d in (rz.get("skipped_details") or [])]
        errs = [f"сделка {d['lead_id']}: {d.get('error', '')[:80]}" for d in (rz.get("error_details") or [])]
        if errs:
            parts += ["  ⚠️ ошибки:"] + bullets(errs)
        elif warns:
            parts += ["  ⏸ пропуски:"] + bullets(warns[:4])

    if ch is None:
        parts.append("💬 Чаты Авито: прогон не отработал (см. лог GitHub)")
    else:
        parts.append(f"💬 Чаты: обработано {ch['acted']}, пропущено {ch['skipped']}, ошибок {ch['errors']} "
                     f"(заявок в лотке {ch['unsorted_chats']})")
        parts += bullets([fmt_chat(d) for d in ch.get("details") or []])
        sk = [d.get("reason", "")[:70] for d in (ch.get("skipped_details") or [])]
        er = [d.get("error", "")[:80] for d in (ch.get("error_details") or [])]
        if er:
            parts += ["  ⚠️ ошибки:"] + bullets(er)
        elif sk:
            parts += ["  ⏸ пропуски:"] + bullets(sk[:4])

    note = "\n".join(parts)
    print(note)

    if DRY or not JOURNAL_LEAD_ID:
        return
    token = os.environ["AMO_TOKEN"]
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
