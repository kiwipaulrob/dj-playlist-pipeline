#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""
KEXP Midday Show → Music Assistant Playlist (monthly aggregate).

Fetches ALL episodes of the Midday Show (program 15) in a month via the public
KEXP API (api.kexp.org/v2), merges + dedupes trackplays, searches MA per track
(library first, then cross-provider), creates ONE playlist per month named
'KEXP - {Month YYYY} - The Midday Show'. Supports --fill, --resume, --dry-run.

Station-specific half: ALL Music Assistant plumbing lives in ma_playlist_lib.py
(shared with dandelion-to-ma.py — L8 consolidation, 15 Aug 2026). This file
keeps the KEXP-only parts: the public-API client (_kexp_get), episode fetch +
merge (kexp_month_tracks/_merge_episodes), and the show-name resolution.

History:
  v3 (15 Aug 2026) — provider priority, pagination, fill hardening (h-m);
    post-run verify wired into the LIVE create path (M2).
  v4 (15 Aug 2026) — L8: shared ma_playlist_lib.py; L1: search timeouts
    counted separately from other errors; L0: create path passes int(pid).
"""

import argparse
import json
import re
import sys
import time
import urllib.request

import ma_playlist_lib as mplib
import dandelion_dash_lib as ddlib

KEXP_API = "https://api.kexp.org/v2"
KEXP_PROGRAM_NAME = "The Midday Show"


def _kexp_get(path, timeout=30):
    req = urllib.request.Request(f"{KEXP_API}{path}", headers={"User-Agent": "hermes-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def kexp_month_tracks(month, dj=None):
    """All trackplays in `month` for (optionally) a show/DJ name, merged →
    deduped {artist, title} list.

    Airdate-window access path (15 Aug 2026): /plays/ IGNORES show filters
    (each call returns the newest ~300 plays — the old code silently built
    every month from that window). We query the month's airdate window
    (ddlib.kexp_play_walk, ~13 pages) and filter per-show client-side by
    numeric show id (ddlib.kexp_episode_ids: matches program_name OR
    host_names, case-insensitive substring). Returns None on API failure
    (fail-loud — callers must abort, not build an empty/partial playlist).
    """
    show_ids = None
    if dj:
        show_ids = ddlib.kexp_episode_ids(month, dj)
        if show_ids is None:
            return None
        if not show_ids:
            # Distinguish "API found no matching episodes" from "plays missing"
            # — pre-fix these collapsed into a misleading "No trackplays found".
            print(f"  ⚠️  No KEXP episodes matched '{dj}' in {month} "
                  f"(full /shows/ archive walked) — nothing to build")
            return []
    plays = ddlib.kexp_play_walk(month, show_ids=show_ids)
    if plays is None:
        return None
    seen, tracks = set(), []
    for artist, title in plays:
        key = f"{artist.lower()}|{title.lower()}"
        if key in seen:
            continue
        seen.add(key)
        tracks.append({"artist": artist, "title": title})
    return tracks


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--month', required=True, help='YYYY-MM')
    p.add_argument('--dj', help='Filter by show name or DJ (partial match)')
    p.add_argument('--ma-host', default='192.168.214.159')
    p.add_argument('--delay', type=float, default=0.1)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--fill', action='store_true')
    args = p.parse_args()

    mplib.MA_HOST = args.ma_host
    token = mplib.get_ma_token()
    if not token:
        print("❌ No MA_TOKEN"); sys.exit(1)
    try:
        y, m = args.month.split('-')
        m_int = int(m)
        if m_int < 1 or m_int > 12 or len(m) != 2:
            raise ValueError
    except (ValueError, AttributeError):
        print(f"❌ Invalid month '{args.month}' — expected YYYY-MM")
        sys.exit(1)
    mn = mplib.MONTHS[m_int]

    # playlist display name: the selected show/DJ (default: The Midday Show)
    show_name = KEXP_PROGRAM_NAME
    if args.dj:
        d = _kexp_get("/programs/?limit=200")
        for pr in (d.get("results") or []):
            if args.dj.lower() in (pr.get("name") or "").lower():
                show_name = pr["name"]
                break
        else:
            show_name = args.dj.strip()

    print(f"\n{'═'*55}")
    print(f"  KEXP → Music Assistant")
    print(f"  {show_name} · {mn} {y}  | MA: {mplib.MA_HOST}")
    mode = 'DRY-RUN' if args.dry_run else ('FILL' if args.fill else 'LIVE')
    print(f"  {mode}")
    print(f"{'═'*55}\n")

    print("📡 Fetching KEXP plays...")
    tracks = kexp_month_tracks(args.month, dj=args.dj)
    if tracks is None:
        print(f"❌ KEXP API failure while fetching {mn} {y} plays — aborting (fail-loud)")
        sys.exit(2)
    if not tracks:
        print(f"❌ No trackplays found for {show_name} in {mn} {y}")
        sys.exit(1)
    print(f"  {len(tracks)} unique tracks across the month")

    canonical = f"kexp - {mn} {y} - {show_name}".lower()

    # FILL: diff into existing playlist
    if args.fill:
        pls = mplib.fetch_all_playlists(token)   # L3: single paginated fetch (was 2 calls)
        if pls is None:
            sys.exit(2)
        ids = [str(p.get('item_id')) for p in pls
               if re.sub(r'\s*\(\d+\)\s*$', '', (p.get('name') or '').lower().strip()) == canonical]
        if not ids:
            print(f"⏭️  No existing '{KEXP_PROGRAM_NAME}' playlist for {mn} {y} — use LIVE mode")
            sys.exit(1)
        pid = ids[0]
        present = mplib.playlist_track_keys(token, pid)
        if present is None:
            print(f"    ❌ Could not read existing tracks for id={pid} — aborting fill (avoid duplicate adds)")
            sys.exit(2)
        missing = [t for t in tracks if mplib.norm_key(t['artist'], t['title']) not in present]
        print(f"  Playlist id={pid}: {len(tracks)} expected, "
              f"{len(tracks) - len(missing)} present, {len(missing)} missing")
        new_uris, t0 = [], time.time()
        still_missing = []   # (artist, title, reason) — recorded after the loop
        found_keys = []      # (dj, artist, title) cleared from the store on success
        for i, t in enumerate(missing):
            if i % 10 == 0:
                print(f"  [hb {time.time()-t0:6.0f}s] {i}/{len(missing)} missing", flush=True)
            m = mplib.search_ma(token, t['artist'], t['title'])
            if m:
                new_uris.append(m[0])
                found_keys.append((show_name, t['artist'], t['title']))
            else:
                still_missing.append((t['artist'], t['title'],
                                      mplib.LAST_SEARCH_REASON or "no_match"))
            time.sleep(args.delay)
        # Feature 1 reconciliation: found → clear from store; still-missing →
        # upsert (bumps attempts; retryable reasons upgrade the stored one).
        mplib.clear_unavailable_many("kexp", args.month, found_keys)
        mplib.record_unavailable_many(
            "kexp", args.month,
            [(show_name, a, ti, r) for a, ti, r in still_missing])
        if found_keys or still_missing:
            print(f"  ↳ unavailable-store: {len(found_keys)} cleared, "
                  f"{len(still_missing)} recorded")
        print(f"  → found {len(new_uris)}/{len(missing)}")
        if args.dry_run:
            print(f"  (dry-run) would add {len(new_uris)} to id={pid}")
        elif new_uris:
            mplib.add_to_existing(token, pid, new_uris)
            print(f"  ✅ added {len(new_uris)} to id={pid}")
        return

    # LIVE: search all, create monthly playlist
    existing_names = None
    if args.resume or not args.dry_run:
        print("  Checking existing playlist names...")
        existing_names = mplib.get_existing_playlist_names(token)
        if existing_names is None:
            sys.exit(2)
        if args.resume and canonical in existing_names:
            print(f"⏭️  Playlist exists — skipping (--resume)")
            sys.exit(0)

    print(f"\n🔍 Searching MA ({len(tracks)} tracks)...")
    uris, found, missing = [], [], []
    still_missing = []   # (artist, title, reason) — recorded after the loop
    t0 = time.time()
    for i, t in enumerate(tracks):
        if i % 10 == 0:
            print(f"  [hb {time.time()-t0:6.0f}s] {i}/{len(tracks)}", flush=True)
        m = mplib.search_ma(token, t['artist'], t['title'])
        if m:
            uris.append(m[0])
            found.append(f"✅ {t['artist'][:18]:18s} - {t['title'][:35]:35s} ({m[1]})")
        else:
            missing.append(f"❌ {t['artist'][:18]:18s} - {t['title']}")
            still_missing.append((t['artist'], t['title'],
                                  mplib.LAST_SEARCH_REASON or "no_match"))
        time.sleep(args.delay)
    # Feature 1: persist what never matched (durable across runs; fill mode
    # reconciles this store on later runs).
    mplib.record_unavailable_many(
        "kexp", args.month,
        [(show_name, a, ti, r) for a, ti, r in still_missing])
    if still_missing:
        print(f"  ↳ unavailable-store: {len(still_missing)} recorded")
    print(f"\n  ✅ {len(found)}/{len(tracks)} found")
    for line in found[:15]:
        print(f"    {line}")
    if len(found) > 15:
        print(f"    ... +{len(found)-15} more")
    for line in missing:
        print(f"    {line}")

    if args.dry_run:
        print(f"\n  DRY RUN — {len(found)}/{len(tracks)} would match. No playlists created.")
        print(f"  Search errors: {mplib.SEARCH_ERRORS} (timeouts: {mplib.SEARCH_TIMEOUTS})")
        return

    print(f"🎵 Creating playlist...")
    pname = f"KEXP - {mn} {y} - {show_name}"
    counter = 2
    while pname.lower().strip() in (existing_names or set()):
        pname = f"KEXP - {mn} {y} - {show_name} ({counter})"
        counter += 1
    print(f"  '{pname}' ({len(uris)} tracks)...")
    pid = mplib.create_playlist(token, pname, uris)
    if pid:
        print(f"  ✅ ID={pid} — created '{pname}' with {len(uris)} tracks")
        ok = mplib.wait_for_add_tasks(token, pname)          # M2: parity with dandelion fix B
        if not ok:
            print(f"  ⚠️  Add tasks still pending after wait — tracks may land later")
        actual = mplib.verify_playlist_count(token, pid)   # M2: parity with dandelion fix E
        if actual >= 0:
            mark = "✅" if actual == len(uris) else "⚠️"
            print(f"  {mark} verify: {actual}/{len(uris)} tracks on playlist")
        else:
            print(f"  ⚠️  verify: could not query track count")
        if missing:
            print(f"  ❌ Missing ({len(missing)}):")
            for m in missing[:10]:
                print(f"    {m}")
            if len(missing) > 10:
                print(f"    ... and {len(missing)-10} more")
    else:
        print(f"  ❌ Create failed for '{pname}' — exiting non-zero (L7)")
        sys.exit(2)
    print(f"  Search errors: {mplib.SEARCH_ERRORS} (timeouts: {mplib.SEARCH_TIMEOUTS})")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except mplib.MAMidRunOutage as e:
        print(f"\n❌ ABORTED: {e}", file=sys.stderr)
        sys.exit(3)
