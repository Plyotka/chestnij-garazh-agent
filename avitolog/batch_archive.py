#!/usr/bin/env python3
"""Пакетное удаление (в «Удалённые», восстановимо) объявлений Авито через WebBridge.
Использование: python3 avito_batch_archive.py <file-with-json-array-of-ids>"""
import json, sys, time, urllib.request

DAEMON = "http://127.0.0.1:10086/command"
SESSION = "chg-avitolog"

def cmd(action, args, timeout=30):
    body = json.dumps({"action": action, "args": args, "session": SESSION}).encode()
    req = urllib.request.Request(DAEMON, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        return {"ok": False, "error": str(e)}

urls = json.load(open("/tmp/cga/target_urls.json"))
ids = json.load(open(sys.argv[1]))
results = {}
for iid in ids:
    url = urls.get(iid)
    if not url:
        results[iid] = "no-url"; continue
    cmd("navigate", {"url": url})
    time.sleep(4)
    r = cmd("evaluate", {"code": "(()=>{const b=document.querySelector('[data-marker=\"delete-item-button\"]');if(!b)return 'nobtn';b.click();return 'clicked'})()"})
    val = (r.get("data") or {}).get("value")
    interrupted = "interrupted" in json.dumps(r.get("error") or {})
    if val != "clicked" and not interrupted:
        results[iid] = f"no-delete-button:{val}"; continue
    ok = False
    for attempt in range(4):
        cmd("dialog", {"action": "accept"})
        time.sleep(1.2)
        v = cmd("evaluate", {"code": "(()=>JSON.stringify({ok:/Вы удалили объявление/.test(document.body.innerText||'')}))()"})
        vval = (v.get("data") or {}).get("value", "{}")
        try:
            if json.loads(vval).get("ok"):
                ok = True; break
        except Exception:
            pass
    results[iid] = "deleted" if ok else "unconfirmed"
    print(f"{iid}: {results[iid]}", flush=True)

print("SUMMARY:", json.dumps(results, ensure_ascii=False))
