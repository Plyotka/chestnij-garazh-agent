# -*- coding: utf-8 -*-
"""Ежедневный самоаудит воронки (решение владельца 14.11 «чтобы не было
косяков»). Детерминированно, без LLM. Ищет аномалии, которые раньше
находили глазами:

  - открытые сделки без открытой контрольной задачи (старше 3 ч — свежие
    ещё не обработаны прогоном);
  - открытые задачи на закрытых сделках (142/143);
  - «Приедет» без задачи подтверждения/«Когда приедет?»;
  - просроченные задачи старше 48 ч (ШАГ 0 не справился);
  - сделки «Тёплые/Приедет» без движения 4+ суток (радар застрявших).

Отчёт — заметкой на служебную карточку журнала (JOURNAL_LEAD_ID).
Запуск: workflow chg-audit.yml в 19:40 МСК ежедневно.
"""
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
AMO = "https://autoservice61.amocrm.ru"
TOKEN = os.environ["AMO_TOKEN"]
JOURNAL_LEAD_ID = int(os.environ.get("JOURNAL_LEAD_ID", "0"))
PIPELINE = 11282498
ST = {88470414: "Первичный", 88470418: "Тёплые", 88470422: "Приедет", 88470426: "Приехал"}


def http(method, url, body=None, tries=5):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code in (401, 429, 500, 502, 503) and i < tries - 1:
                time.sleep(10 * (i + 1))
                continue
            raise


def get_all(path):
    out, url = [], AMO + path
    while url:
        r = http("GET", url)
        out += (r.get("_embedded") or {}).get(url.split("?")[0].rsplit("/", 1)[-1] + "s") or \
               (r.get("_embedded") or {}).get("leads") or (r.get("_embedded") or {}).get("tasks") or []
        url = ((r.get("_links") or {}).get("next") or {}).get("href")
    return out


def main():
    now = datetime.now(MSK)
    nowts = int(now.timestamp())
    issues = []

    leads = get_all(f"/api/v4/leads?filter[pipeline_id][]={PIPELINE}&limit=250")
    tasks = get_all("/api/v4/tasks?filter[is_completed]=0&limit=250")

    by_lead = {}
    for t in tasks:
        if t.get("entity_id"):
            by_lead.setdefault(t["entity_id"], []).append(t)

    for l in leads:
        lid, st = l["id"], l.get("status_id")
        if lid == JOURNAL_LEAD_ID:
            continue
        age_h = (nowts - (l.get("created_at") or 0)) / 3600
        idle_d = (nowts - (l.get("updated_at") or 0)) / 86400
        open_t = by_lead.get(lid, [])
        name = (l.get("name") or "")[:50]
        if st in ST and not open_t and age_h > 3:
            issues.append(f"• {lid} «{name}» [{ST[st]}]: нет открытой задачи (создана {age_h:.0f} ч назад)")
        if st in (142, 143) and open_t:
            for t in open_t:
                issues.append(f"• {lid} «{name}» [закрыта]: висит задача до "
                              f"{datetime.fromtimestamp(t.get('complete_till') or 0, MSK):%d.%m} — закрыть")
        if st == 88470422 and open_t:
            if not any(("за день" in (t.get("text") or "")
                        or "когда приедет" in (t.get("text") or "").lower()
                        or "актуален" in (t.get("text") or "").lower()
                        or "приехал" in (t.get("text") or "").lower()) for t in open_t):
                issues.append(f"• {lid} «{name}» [Приедет]: задача есть, но не про приезд")
        if st in (88470418, 88470422) and idle_d >= 4:
            issues.append(f"• {lid} «{name}» [{ST[st]}]: без движения {idle_d:.0f} суток")

    for t in tasks:
        due = t.get("complete_till") or 0
        if due and nowts - due > 48 * 3600:
            issues.append(f"• задача {t['id']} просрочена {(nowts - due) / 86400:.1f} сут: "
                          f"{(t.get('text') or '')[:60]}")

    header = f"[АУДИТ] {now.strftime('%d.%m.%Y')} — "
    if not issues:
        text = header + "аномалий нет, воронка чиста"
        print(text)
    else:
        text = header + f"аномалий: {len(issues)}\n" + "\n".join(issues[:25])
        print(text)
    if JOURNAL_LEAD_ID:
        http("POST", f"{AMO}/api/v4/leads/{JOURNAL_LEAD_ID}/notes",
             [{"note_type": "common", "params": {"text": text}}])
        print("записано в журнал")


if __name__ == "__main__":
    main()
