#!/usr/bin/env python3
"""Wait for the Feb/Apr KEXP rebuild units, then report logs + MA playlist state.

Read-only: polls systemctl, tails run logs, queries the MA API for the two
rebuilt playlists' track counts. No mutations.
"""
import json
import subprocess
import time
import urllib.request

UNITS = ["kexp-rebuild-feb26.service", "kexp-rebuild-apr26.service"]
LOGS = {
    "kexp-rebuild-feb26.service": "/root/.hermes/data/dandelion-runs/kexp-2026-02-rebuild.log",
    "kexp-rebuild-apr26.service": "/root/.hermes/data/dandelion-runs/kexp-2026-04-rebuild.log",
}
WANT = {"kexp - february 2026 - cheryl waters", "kexp - april 2026 - cheryl waters"}
DEADLINE = time.time() + 3 * 3600


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


# 1. wait until both units are no longer active
while time.time() < DEADLINE:
    states = sh("systemctl", "is-active", *UNITS).split()
    if states and all(s in ("inactive", "failed") for s in states):
        break
    time.sleep(30)

print("=== UNIT RESULTS ===")
for u in UNITS:
    print(f"{u}: {sh('systemctl', 'is-active', u)}  (result: {sh('systemctl', 'is-failed', u)})")

# 2. final log tails
for u, log in LOGS.items():
    print(f"\n=== {u} — last 30 log lines ===")
    try:
        with open(log) as f:
            lines = f.read().splitlines()
        print("\n".join(lines[-30:]) or "(empty log)")
    except OSError as e:
        print(f"(log unreadable: {e})")

# 3. MA verification: find the two playlists, count tracks
def ma_token():
    t = ""
    for ln in open("/root/.bashrc"):
        if "MA_TOKEN" in ln and "export" in ln:
            t = ln.split("=", 1)[1].strip().strip('"').strip("'")
            break
    return t


def api(token, payload, timeout=40):
    req = urllib.request.Request(
        "http://192.168.214.159:8095/api", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


print("\n=== MA PLAYLIST VERIFICATION ===")
try:
    tok = ma_token()
    if not tok:
        print("no MA_TOKEN — cannot verify")
    else:
        pls, offset = [], 0
        while True:
            page = api(tok, {"message_id": "w", "command": "music/playlists/library_items",
                             "args": {"limit": 500, "order_by": "name", "offset": offset}}, timeout=30)
            if not isinstance(page, list) or not page:
                break
            pls.extend(page)
            if len(page) < 500:
                break
            offset += 500
        for p in pls:
            n = (p.get("name") or "")
            if n.lower().strip() in WANT:
                tr = api(tok, {"message_id": "v", "command": "music/playlists/playlist_tracks",
                               "args": {"item_id": str(p.get("item_id")),
                                        "provider_instance_id_or_domain": "library"}}, timeout=30)
                cnt = len(tr) if isinstance(tr, list) else -1
                print(f"✅ '{n}' id={p.get('item_id')}: {cnt} tracks")
        found = { (p.get('name') or '').lower().strip() for p in pls } & WANT
        for w in sorted(WANT - found):
            print(f"❌ '{w}' — NOT FOUND in MA library")
except Exception as e:
    print(f"MA verification failed: {e}")
print("\n=== WATCHER DONE ===")
