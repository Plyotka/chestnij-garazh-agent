#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Авито-еженедельник «Честный Гараж» — аналитик витрины (1 LLM-прогон в неделю).

Сбор данных — кодом (Avito stats/spendings + amoCRM), LLM только интерпретирует
готовые цифры и готовит СПИСОК ПРАВОК НА ПОДТВЕРЖДЕНИЕ ВЛАДЕЛЬЦА. Сам ничего
в Авито не меняет (правило: тексты/цены/статусы — только после «сделать»).

Запуск: понедельник 10:00 МСК (workflow avitolog-weekly.yml) или вручную.
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import razbor
from razbor import amo_get, embedded

MSK = timezone(timedelta(hours=3))
ACCOUNT = 12626609
JOURNAL_LEAD_ID = 37575635
OWNER = 14221122
KIMI_URL = "https://api.moonshot.ai/v1/chat/completions"
KIMI_MODEL = os.environ.get("KIMI_MODEL", "kimi-k2.6")

CLIENT_ID = os.environ.get("AVITO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("AVITO_CLIENT_SECRET", "")
MOONSHOT = os.environ.get("MOONSHOT_API_KEY", "")

# лидеры-недотрог по контенту (правило из ЗНАНИЯ-АВИТО 6.5 + передача 05.11)
LEADERS = ["7630081786", "7950155328", "4111014367", "7918815300", "7630192966"]

TITLES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "titles.json")))


def log(msg):
    print(f"[{datetime.now(MSK):%H:%M:%S}] {msg}", flush=True)


def avito_token():
    req = urllib.request.Request(
        "https://api.avito.ru/token",
        data=f"grant_type=client_credentials&client_id={CLIENT_ID}"
             f"&client_secret={CLIENT_SECRET}".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(req, timeout=30))["access_token"]


def avito(api, method="GET", body=None, token=None):
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"https://api.avito.ru{api}",
                data=json.dumps(body).encode() if body is not None else None,
                method=method,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            if e.code in (204,):
                return {}
            raise


def collect_stats(token, days=30):
    to = (datetime.now(MSK) - timedelta(days=1)).strftime("%Y-%m-%d")
    frm = (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d")
    r = avito(f"/stats/v1/accounts/{ACCOUNT}/items", method="POST", token=token,
              body={"dateFrom": frm, "dateTo": to,
                    "fields": ["uniqViews", "uniqContacts", "uniqFavorites"],
                    "periodGrouping": "day"})
    agg = {}
    for it in r.get("result", {}).get("items", []):
        a = agg.setdefault(it["itemId"], {"views": 0, "contacts": 0, "favs": 0})
        for p in it.get("stats", []):
            a["views"] += p.get("uniqViews", 0)
            a["contacts"] += p.get("uniqContacts", 0)
            a["favs"] += p.get("uniqFavorites", 0)
    return agg


def collect_spendings(token, days=30):
    to = (datetime.now(MSK) - timedelta(days=1)).strftime("%Y-%m-%d")
    frm = (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        socket.setdefaulttimeout(15)
        r = avito(f"/stats/v2/accounts/{ACCOUNT}/spendings", method="POST", token=token,
                  body={"dateFrom": frm, "dateTo": to, "periodGrouping": "day"})
        socket.setdefaulttimeout(60)
        total = 0.0
        for it in r.get("result", {}).get("items", []):
            total += it.get("spent", 0) or 0
        return total
    except Exception as e:
        log(f"spendings недоступны: {e}")
        return None


def collect_crm(days=7):
    """Сделки Авито за период: исходы + тематика закрытий «Не наш профиль»."""
    frm = int((datetime.now(MSK) - timedelta(days=days)).timestamp())
    out = {"closed_not_profile": [], "won": 0, "open_now": 0}
    page = 1
    while True:
        r = razbor.http("GET",
            f"{razbor.AMO_BASE}/api/v4/leads?limit=250&page={page}"
            f"&filter[pipeline_id][]=11282498&filter[source_id][]=22567523"
            f"&filter[updated_at][from]={frm}", token=razbor.AMO_TOKEN)
        leads = embedded(r, "leads")
        if not leads:
            break
        for l in leads:
            st = l.get("status_id")
            if st == 142:
                out["won"] += 1
            elif st == 143:
                if l.get("loss_reason_id") == 24763846:
                    out["closed_not_profile"].append(l.get("name") or f"сделка {l['id']}")
            else:
                out["open_now"] += 1
        if page >= (r.get("_page_count") or 1):
            break
        page += 1
        time.sleep(0.3)
    return out


def kimi(prompt):
    req = urllib.request.Request(
        KIMI_URL,
        data=json.dumps({
            "model": KIMI_MODEL, "temperature": 1,
            "messages": [{"role": "user", "content": prompt}]}).encode(),
        headers={"Authorization": f"Bearer {MOONSHOT}",
                 "Content-Type": "application/json"})
    last = None
    for attempt in range(2):
        try:
            r = json.load(urllib.request.urlopen(req, timeout=600))
            return r["choices"][0]["message"]["content"]
        except Exception as e:
            last = e
            log(f"LLM попытка {attempt + 1} не удалась: {e}")
    raise RuntimeError(f"LLM недоступен: {last}")


def main():
    if not MOONSHOT:
        raise RuntimeError("MOONSHOT_API_KEY не задан")
    token = avito_token()

    log("собираю статистику 30д")
    stats = collect_stats(token)
    spent = collect_spendings(token)
    crm = collect_crm()

    total_c = sum(v["contacts"] for v in stats.values())
    total_v = sum(v["views"] for v in stats.values())
    cr = f"{total_c / total_v * 100:.1f}" if total_v else "0"
    cpl = f"{spent / total_c:.0f}" if spent and total_c else "н/д"

    cards = []
    for iid, v in sorted(stats.items(), key=lambda kv: -kv[1]["contacts"]):
        cards.append({
            "id": iid, "title": TITLES.get(str(iid), "(нет в выгрузке — новая?)"),
            "views": v["views"], "contacts": v["contacts"], "favs": v["favs"],
            "leader": str(iid) in LEADERS})
    dead = [c for c in cards if c["contacts"] == 0 and c["views"] > 50]
    rows = [c for c in cards if c["views"] >= 20 or c["contacts"] > 0][:60]

    prompt = f"""Ты — авитолог автосервиса «Честный Гараж» (Ростов-на-Дону, Фурмановская 150Б),
тариф CPA. Ниже цифры за 30 дней по {len(rows)} карточкам и исходы CRM за 7 дней.

КАРТОЧКИ (id, заголовок, просмотры, контакты, избранное; leader=true — НЕ ТРОГАТЬ контент):
{json.dumps(rows, ensure_ascii=False)}

ИТОГО: контактов {total_c}, просмотров {total_v}, CR {cr}%, расход {spent} ₽, CPL {cpl} ₽.
CRM за 7 дней: успехов {crm['won']}, закрыто «Не наш профиль» {len(crm['closed_not_profile'])},
открыто сейчас {crm['open_now']}.
Названия закрытых «не профиль» (что просили): {json.dumps(crm['closed_not_profile'][:40], ensure_ascii=False)}

ЗАДАЧА: подготовь список правок витрины СТРОГО на подтверждение владельца.
Правила: лидеров не трогаем; перевыкладку не предлагаем (фильтр дублей); только
правки заголовков/описаний/цен и архив явного мусора; каждая правка — с обоснованием
цифрами. Формат ответа — JSON-массив:
[{{"id": "...", "action": "правка заголовка|правка описания|правка цены|архив|оставить",
  "now": "…", "propose": "…", "why": "…"}}]
Не больше 8 правок, только где эффект очевиден. Если правок не требуется — пустой массив."""
    log("LLM-анализ")
    answer = kimi(prompt)
    try:
        start = answer.find("[")
        proposals = json.loads(answer[start:answer.rfind("]") + 1])
    except Exception:
        proposals = None

    now = datetime.now(MSK)
    note = (f"Авито-еженедельник {now:%d.%m}: контактов 30д {total_c}, CR {cr}%, "
            f"CPL {cpl} ₽, успехов 7д {crm['won']}, «не профиль» 7д {len(crm['closed_not_profile'])}")
    if dead:
        note += f"; мёртвые (просмотры есть, контактов 0): {[c['id'] for c in dead][:10]}"
    razbor.amo("POST", f"/api/v4/leads/{JOURNAL_LEAD_ID}/notes",
               [{"note_type": "common", "params": {"text": note}}])
    log(note)

    if proposals:
        lines = []
        for p in proposals:
            title = next((c["title"] for c in cards if c["id"] == p["id"]), "?")[:40]
            lines.append(f"• {p['id']} ({title}): {p['action']} — {str(p.get('propose',''))[:120]}"
                         f"\n  почему: {str(p.get('why',''))[:200]}")
        body = (f"Kimi: правки Авито на подтверждение ({len(proposals)} шт).\n" + "\n".join(lines)
                + "\nОтветь «сделать <id>» / «отклонить <id>» — применю через автозагрузку.")
        razbor.amo("POST", "/api/v4/tasks", [{
            "text": body[:4000], "responsible_user_id": OWNER,
            "complete_till": int((now + timedelta(days=3)).timestamp())}])
        log(f"задача владельцу: {len(proposals)} правок")
    else:
        log("правок не предложено или LLM-ответ не разобран")
        razbor.amo("POST", "/api/v4/leads/{JOURNAL_LEAD_ID}/notes".format(
            JOURNAL_LEAD_ID=JOURNAL_LEAD_ID),
            [{"note_type": "common", "params": {"text": note + " | правки: не предложены"}}])


if __name__ == "__main__":
    main()
