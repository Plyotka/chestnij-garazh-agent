#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Авито-монитор «Честный Гараж» — жизнеобеспечение витрины без LLM (только API).

Что делает каждый прогон (в окне 09:00–20:30 МСК):
1. Баланс кошелька → ниже порога = задача владельцу пополнить.
2. Состояние всех карточек (status + finish_time) → обновляет avitolog/state.json.
3. Свежие истёкшие карточки → активация (PUT update_status active, CPA — бесплатно,
   только ПОСЛЕ истечения; до истечения Авито отбивает — проверено 05.11).
4. Контакты за вчера по stats API → 0 в будний день = аномалия, задача владельцу.
5. Краткая заметка в журнал CRM (карточка JOURNAL_LEAD_ID).

Всё чтение/запись — кодом, 0 токенов LLM. LLM-анализ — отдельный еженедельный
прогон (avitolog/weekly.py), только после утверждения списка правок владельцем.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import razbor
from razbor import amo, amo_get, embedded

MSK = timezone(timedelta(hours=3))
ACCOUNT = 12626609
JOURNAL_LEAD_ID = 37575635          # «Kimi — журнал прогонов» — служебная карточка
OWNER = 14221122                    # Константин
BALANCE_MIN = 2000                  # порог алерта, ₽ (простой кошелька ≈ потеря лидов)
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

CLIENT_ID = os.environ.get("AVITO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("AVITO_CLIENT_SECRET", "")


def log(msg):
    print(f"[{datetime.now(MSK):%H:%M:%S}] {msg}", flush=True)


def in_window(now):
    return (9, 0) <= (now.hour, now.minute) <= (20, 30)


def avito_token():
    req = urllib.request.Request(
        "https://api.avito.ru/token",
        data=f"grant_type=client_credentials&client_id={CLIENT_ID}"
             f"&client_secret={CLIENT_SECRET}".encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return json.load(urllib.request.urlopen(req, timeout=30))["access_token"]


def avito(api, method="GET", body=None, token=None):
    req = urllib.request.Request(
        f"https://api.avito.ru{api}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=40))


def load_state():
    try:
        return json.load(open(STATE_PATH))
    except Exception:
        return {}


def save_state(st):
    st["saved_at"] = datetime.now(MSK).isoformat()
    json.dump(st, open(STATE_PATH, "w"), ensure_ascii=False, indent=1)


def owner_task(text, due_h=18):
    """Одна открытая задача на владельца по теме — не плодим дубли."""
    now = datetime.now(MSK)
    due = now.replace(hour=due_h, minute=0, second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    resp = razbor.http("GET",
        f"{razbor.AMO_BASE}/api/v4/tasks?filter[is_completed]=0&limit=250",
        token=razbor.AMO_TOKEN)
    for t in embedded(resp, "tasks"):
        if t.get("responsible_user_id") == OWNER and (t.get("text") or "").startswith(text[:40]):
            return
    amo("POST", "/api/v4/tasks", [{
        "text": text, "responsible_user_id": OWNER,
        "complete_till": int(due.timestamp())}])


def journal(text):
    amo("POST", f"/api/v4/leads/{JOURNAL_LEAD_ID}/notes",
        [{"note_type": "common", "params": {"text": text}}])


def main():
    now = datetime.now(MSK)
    if not in_window(now):
        log("вне окна 09:00–20:30 — выход")
        return
    if not CLIENT_ID or not razbor.AMO_TOKEN:
        raise RuntimeError("AVITO_CLIENT_ID/AMO_TOKEN не заданы")

    token = avito_token()
    state = load_state()
    prev_items = state.get("items") or {}
    anomalies = []

    # 1. баланс
    bal = avito(f"/core/v1/accounts/{ACCOUNT}/balance/", token=token)
    real = bal.get("real", 0)
    log(f"баланс: {real} ₽")
    if real < BALANCE_MIN:
        anomalies.append(f"кошелёк {real} ₽ < {BALANCE_MIN} ₽ — простой ≈ потеря лидов")
        owner_task(f"Kimi: пополнить кошелёк Авито ({real} ₽, минимум {BALANCE_MIN} ₽). "
                   f"Идеально — включить автопополнение в кабинете.")

    # 2. список id карточек из stats (вселенная: активные + недавно снятые)
    y = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    stats = avito(f"/stats/v1/accounts/{ACCOUNT}/items", method="POST", token=token,
                  body={"dateFrom": y, "dateTo": y,
                        "fields": ["uniqContacts"], "periodGrouping": "day"})
    ids = sorted({it["itemId"] for it in stats.get("result", {}).get("items", [])
                  if it.get("itemId")} | set(prev_items.keys()) | set(state.get("all_ids") or []))
    # заодно стата вчера: контакты и просмотры
    y_contacts = sum(p.get("uniqContacts", 0)
                     for it in stats.get("result", {}).get("items", [])
                     for p in it.get("stats", []))
    y_views = sum(p.get("uniqViews", 0)
                  for it in stats.get("result", {}).get("items", [])
                  for p in it.get("stats", []))

    # 3. состояние карточек. Активация через API НЕ существует (все роуты 404,
    #    проверено 06.11) — оживление только кнопкой в кабинете. Значит: ловим
    #    истёкшие и ставим владельцу задачу с пошаговой инструкцией (один клик).
    cur = {}
    expired_now, expiring_today = [], []
    today_end = now.replace(hour=23, minute=59).isoformat()
    for iid in ids:
        try:
            info = avito(f"/core/v1/accounts/{ACCOUNT}/items/{iid}", token=token)
            cur[iid] = {"status": info.get("status"),
                        "finish": info.get("finish_time")}
        except urllib.error.HTTPError as e:
            cur[iid] = {"status": f"err{e.code}", "finish": None}
        time.sleep(0.12)
    for iid, c in cur.items():
        prev = prev_items.get(iid) or {}
        was_live = prev.get("status") == "active" or (
            not prev and (c.get("finish") or "") > now.isoformat())
        # тревога и на свежепавших, и на тех, кто лежит трупом с прошлого прогона
        # (статус "old" = истёк срок; пока не оживили — задача висит, дубль не создаётся)
        if (was_live and c.get("status") != "active") or c.get("status") == "old":
            expired_now.append(iid)
        elif c.get("status") == "active" and c.get("finish") and \
                now.isoformat() < c["finish"] <= today_end:
            expiring_today.append(iid)
    if expired_now:
        anomalies.append(f"истекли и ждут оживления: {expired_now}")
        owner_task(f"Kimi: истекли карточки Авито {expired_now}. Оживить: профиль → "
                   f"«Истёк срок размещения» → «Опубликовать» (бесплатно, на 30 дней). "
                   f"Простой = потеря контактов (~4/день на карточку).")
    if expiring_today:
        log(f"истекают сегодня: {expiring_today}")

    # 4. аномалия: 0 контактов в будний день
    weekday = now.weekday() < 5
    if weekday and y_contacts == 0 and (datetime.now(MSK).hour >= 10):
        anomalies.append("0 контактов за вчера (будний день)")
        owner_task("Kimi: вчера 0 контактов по Авито (будний день) — проверить "
                   "кошелёк/блокировки/статусы объявлений.")

    # 5. журнал
    note = (f"Авито-монитор {now:%d.%m %H:%M}: карт {len(cur)} (active "
            f"{sum(1 for c in cur.values() if c.get('status')=='active')}), "
            f"баланс {real} ₽, вчера контактов {y_contacts} / просм {y_views}")
    if expired_now:
        note += f"; ИСТЕКЛИ (ждут оживления): {expired_now}"
    if expiring_today:
        note += f"; истекают сегодня: {expiring_today}"
    if anomalies:
        note += "; АНОМАЛИИ: " + " | ".join(anomalies)
    log(note)
    journal(note)

    state["items"] = cur
    state["all_ids"] = ids
    hist = state.get("contacts_hist") or []
    hist.append({"date": y, "contacts": y_contacts, "views": y_views})
    state["contacts_hist"] = hist[-45:]
    save_state(state)


if __name__ == "__main__":
    main()
