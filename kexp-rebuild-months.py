#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""Rebuild KEXP monthly playlists with TRUE month content (airdate-window).

v2 (15 Aug 2026) — deterministic two-phase sync. v1's add-then-prune raced
MA's background add/remove tasks: removes targeted wrong positions and some
add tasks failed, leaving every playlist MIXED (extras + missing). v2 removes
ALL current content first (full position range, playlist quiescent — no
concurrent mutations), verifies EMPTY, then adds the true month, verifies.

Guarantees:
  - playlist ids/names preserved; MA favourites untouched (library-level);
  - fail-loud ordering: true-month tracks are fetched AND searched BEFORE any
    mutation (an API failure never empties a playlist);
  - search results cached to /root/.hermes/cache/kexp-rebuild-uris-<month>.json
    so re-runs/repairs skip the ~1,800-track search pass;
  - every add/remove phase waits until no pending/running KEXP tasks remain
    (tasks/list poll, not the fixed 300s wait).
"""
import sys, time, re, json, os, argparse, importlib.util

sys.path.insert(0, "/root/.hermes/scripts")
import ma_playlist_lib as mplib
import dandelion_dash_lib as ddlib

spec = importlib.util.spec_from_file_location(
    "kexp_to_ma",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "kexp-to-ma.py"))
kexp_to_ma = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kexp_to_ma)

DJ = "Cheryl Waters"
CACHE_DIR = "/root/.hermes/cache"
token = mplib.get_ma_token()

ap = argparse.ArgumentParser(
    description="Rebuild KEXP monthly playlists from the true airdate window. "
                "Months as YYYY-MM args; default = previous month.")
ap.add_argument("months", nargs="*",
                help="months to rebuild, YYYY-MM (default: previous month)")
ap.add_argument("--dj", default=DJ, help=f"show host (default: {DJ})")
args = ap.parse_args()

now = time.gmtime()
tot = now.tm_year * 12 + now.tm_mon - 1
prev = f"{tot // 12:04d}-{tot % 12 + 1:02d}"
MONTHS = []
for m in (args.months or [prev]):
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", m):
        ap.error(f"bad month {m!r} — use YYYY-MM (01-12)")
    MONTHS.append((m, mplib.MONTHS[int(m.split("-")[1])]))


def wait_kexp_tasks(max_wait=600):
    """Poll tasks/list until no pending/running task mentions a KEXP playlist."""
    waited = 0
    while waited < max_wait:
        try:
            tasks = mplib._api(token, {"message_id": "tw", "command": "tasks/list", "args": {}}, timeout=20)
        except Exception:
            return False
        if isinstance(tasks, list):
            busy = [t for t in tasks
                    if (t.get("name") or "").startswith(("Add ", "Remove "))
                    and "KEXP" in (t.get("name") or "")
                    and t.get("status") in ("pending", "running")]
            if not busy:
                return True
        time.sleep(10)
        waited += 10
    return False


fav_before = len(ddlib.get_favourites()[0])
print(f"Favourites before: {fav_before}")

pls = mplib.fetch_all_playlists(token)
if pls is None:
    sys.exit(2)
by_name = {re.sub(r"\s*\(\d+\)\s*$", "", (p.get("name") or "").lower().strip()): p for p in pls}

for month, mname in MONTHS:
    y = month.split("-")[0]
    canonical = f"kexp - {mname} {y} - {args.dj}".lower()
    pl = by_name.get(canonical)
    if not pl:
        print(f"⏭️  {month}: playlist not found ({canonical!r}) — skipping")
        continue
    pid = str(pl["item_id"])
    print(f"\n{'═' * 55}\n  {mname} {y} · pid={pid}\n{'═' * 55}")

    # 1. true month tracks (fetch BEFORE any mutation)
    tracks = kexp_to_ma.kexp_month_tracks(month, dj=args.dj)
    if tracks is None:
        print(f"❌ {mname}: KEXP API failure — ABORTING (no mutation)")
        sys.exit(2)
    print(f"  true month: {len(tracks)} unique tracks")

    # 2. search (or reuse cached uris)
    cache_path = f"{CACHE_DIR}/kexp-rebuild-uris-{month}.json"
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            uris = json.load(f)
        print(f"  uris from cache: {len(uris)}")
    else:
        uris, t0 = [], time.time()
        for i, t in enumerate(tracks):
            if i % 10 == 0:
                print(f"  [hb {time.time() - t0:6.0f}s] search {i}/{len(tracks)}", flush=True)
            m = mplib.search_ma(token, t["artist"], t["title"])
            if m:
                uris.append(m[0])
            time.sleep(0.05)
        with open(cache_path, "w") as f:
            json.dump(uris, f)
        print(f"  → found {len(uris)}/{len(tracks)} (cached)")
    if mplib.SEARCH_ERRORS:
        print(f"  Search errors: {mplib.SEARCH_ERRORS} (timeouts: {mplib.SEARCH_TIMEOUTS})")

    # 3. remove ALL current content (playlist quiescent — no concurrent adds)
    tr = mplib._api(token, {"message_id": "r1", "command": "music/playlists/playlist_tracks",
                            "args": {"item_id": pid, "provider_instance_id_or_domain": "library"}}, timeout=30)
    if not isinstance(tr, list):
        print(f"❌ {mname}: playlist_tracks failed — ABORTING (no mutation)")
        sys.exit(2)
    n_cur = len(tr)
    print(f"  current content: {n_cur} tracks")
    if n_cur:
        positions = sorted({t.get("position") for t in tr if t.get("position") is not None}, reverse=True)
        if len(positions) != n_cur:
            print(f"  ⚠️  positions ({len(positions)}) != tracks ({n_cur}) — removing by full range instead")
            positions = list(range(n_cur - 1, -1, -1))
        mplib.remove_positions_from_existing(token, pid, positions)
        if not wait_kexp_tasks():
            print(f"  ❌ {mname}: remove tasks did not settle — ABORTING (playlist mid-mutation)")
            sys.exit(2)
        tr = mplib._api(token, {"message_id": "r2", "command": "music/playlists/playlist_tracks",
                                "args": {"item_id": pid, "provider_instance_id_or_domain": "library"}}, timeout=30)
        left = len(tr) if isinstance(tr, list) else -1
        print(f"  post-remove: {left} tracks")
        if left != 0:
            print(f"  ❌ {mname}: playlist not empty after remove-all — ABORTING (investigate before adding)")
            sys.exit(2)

    # 4. add the true month
    if uris:
        mplib.add_to_existing(token, pid, uris)
        if not wait_kexp_tasks():
            print(f"  ⚠️  {mname}: add tasks did not settle in 600s — count may be mid-flight")
    actual = mplib.verify_playlist_count(token, pid)
    mark = "✅" if actual == len(uris) else "⚠️"
    print(f"  {mark} verify: {actual} tracks (expected {len(uris)} = found uris)")

fav_after = len(ddlib.get_favourites()[0])
print(f"\nFavourites: {fav_before} → {fav_after} {'✅ unchanged' if fav_after == fav_before else '❌ CHANGED'}")
print(f"  Search errors: {mplib.SEARCH_ERRORS} (timeouts: {mplib.SEARCH_TIMEOUTS})")
