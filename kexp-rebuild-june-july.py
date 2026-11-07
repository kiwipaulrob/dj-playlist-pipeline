#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""Rebuild KEXP June and July monthly playlists safely using add_to_existing in chunks of 50."""
import sys, time, re, json, os, importlib.util

sys.path.insert(0, "/root/.hermes/scripts")
import ma_playlist_lib as mplib
import dandelion_dash_lib as ddlib

spec = importlib.util.spec_from_file_location("kexp_to_ma", "/root/.hermes/scripts/kexp-to-ma.py")
kexp_to_ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kexp_to_ma)

DJ = "Cheryl Waters"
MONTHS = [("2026-06", "June"), ("2026-07", "July")]
CACHE_DIR = "/root/.hermes/cache"
token = mplib.get_ma_token()

fav_before = len(ddlib.get_favourites()[0])
print(f"Favourites before: {fav_before}")

pls = mplib.fetch_all_playlists(token)
by_name = {re.sub(r"\s*\(\d+\)\s*$", "", (p.get("name") or "").lower().strip()): p for p in pls}

for month, mname in MONTHS:
    canonical = f"kexp - {mname} 2026 - {DJ}".lower()
    pl = by_name.get(canonical)
    if not pl:
        print(f"⏭️  {mname}: playlist not found ({canonical})")
        continue
    pid = str(pl["item_id"])
    print(f"\n{'═' * 55}\n  {mname} 2026 · pid={pid}\n{'═' * 55}")

    tracks = kexp_to_ma.kexp_month_tracks(month, dj=DJ)
    print(f"  true month: {len(tracks)} unique tracks")

    cache_path = f"{CACHE_DIR}/kexp-rebuild-uris-{month}.json"
    if not os.path.exists(cache_path):
        uris = []
        for t in tracks:
            m = mplib.search_ma(token, t["artist"], t["title"])
            if m:
                uris.append(m[0])
            time.sleep(0.02)
        with open(cache_path, "w") as f:
            json.dump(uris, f)
    else:
        with open(cache_path) as f:
            uris = json.load(f)
    print(f"  uris loaded: {len(uris)}")

    tr = mplib._api(token, {"message_id": "r1", "command": "music/playlists/playlist_tracks",
                            "args": {"item_id": pid, "provider_instance_id_or_domain": "library"}}, timeout=30)
    n_cur = len(tr) if isinstance(tr, list) else 0
    print(f"  current content: {n_cur} tracks")
    if n_cur:
        positions = list(range(n_cur - 1, -1, -1))
        mplib.remove_positions_from_existing(token, pid, positions)
        time.sleep(3)

    print(f"  adding {len(uris)} tracks in chunks of 50...")
    for i in range(0, len(uris), 50):
        chunk = uris[i:i+50]
        mplib.add_to_existing(token, pid, chunk)
        time.sleep(0.5)

    actual = mplib.verify_playlist_count(token, pid)
    print(f"  ✅ verify: {actual} tracks (expected {len(uris)})")

fav_after = len(ddlib.get_favourites()[0])
print(f"\nFavourites: {fav_before} → {fav_after} {'✅ unchanged' if fav_after == fav_before else '❌ CHANGED'}")
print("ALL DONE!")
