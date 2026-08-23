#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""
Dandelion Radio Tracklist → Music Assistant Playlist
Uses Scrapling for HTML parsing (CSS selectors instead of regex hacks).

Station-specific half: ALL Music Assistant plumbing lives in ma_playlist_lib.py
(shared with kexp-to-ma.py — L8 consolidation, 15 Aug 2026). This file keeps
the Dandelion-only parts: tracklist scraping (scrape_month/unent), the
dandelion-only get_playlist_map helper, and the CLI/main loop.

History:
  v2 (13 Aug 2026) — review hardening a-g: --resume, heartbeats, add-task
    wait, post-run verify, fail-loud names, --ma-host, 3s write pacing,
    --dry-run.
  v3 (15 Aug 2026) — provider priority (local→Deezer→Bandcamp→Spotify, BBC
    removed), pagination past MA's 500 cap, fill fail-loud (exit 2).
  v4 (15 Aug 2026) — L8: shared ma_playlist_lib.py; L1: search timeouts
    counted separately from other errors; L0: create path passes int(pid).
"""

import argparse
import re
import sys
import time

from scrapling.fetchers import Fetcher

import ma_playlist_lib as mplib


def unent(s):
    return (s.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
             .replace('&nbsp;', ' ').replace('&#x27;', "'").replace('&#39;', "'")
             .replace('&quot;', '"')).strip()


def scrape_month(month):
    """Return {dj_name: [{artist, title, recording, label, catalog}]}."""
    url = f"https://www.dandelionradio.com/tracklists/{month}/main.htm"
    print(f"  Fetching {url}...")
    try:
        page = Fetcher.get(url)
    except Exception as e:
        print(f"  ❌ Failed to fetch {url}: {e}")
        return {}

    sections = {}
    current_dj = None

    for tr in page.css('tr'):
        # Check for DJ header: <b>DJ Name - Month YYYY</b>
        # Only tdblue cells with an anchor contain DJ headers — not plain <b> in track rows
        b_tag = tr.css('td.tdblue b')
        if b_tag:
            # Strip HTML tags from the DJ name
            raw = b_tag[0].html_content.strip()
            clean = re.sub(r'<[^>]+>', '', raw).strip()
            m = re.match(r'(.+?)\s*-\s*(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}', clean)
            if m:
                current_dj = m.group(1).strip()
                if current_dj not in sections:
                    sections[current_dj] = []
                continue

        # Skip header rows
        if tr.css('td.tdheadings'):
            continue

        # Track rows: <td><a href="...">Artist</a></td><td>Title</td>...
        tds = tr.css('td')
        if len(tds) >= 2:
            artist = tds[0].css('::text').get()
            title = tds[1].css('::text').get()
            if artist and title:
                artist = unent(artist.strip())
                title = unent(title.strip())
                if artist and title and artist not in ('Artist', '&nbsp;', '') and title not in ('&nbsp;', ''):
                    recording = unent(tds[2].css('::text').get() or '').strip() if len(tds) > 2 else ''
                    label = unent(tds[3].css('::text').get() or '').strip() if len(tds) > 3 else ''
                    catalog = unent(tds[4].css('::text').get() or '').strip() if len(tds) > 4 else ''
                    if current_dj:
                        sections[current_dj].append({
                            'artist': artist, 'title': title,
                            'recording': recording, 'label': label, 'catalog': catalog
                        })

    # Remove empty sections
    return {k: v for k, v in sections.items() if v}


def get_playlist_map(token):
    """Return {lowercase_name: [item_id, ...]} — fail-loud like names."""
    pls = mplib.fetch_all_playlists(token)
    if pls is None:
        return None
    out = {}
    for p in pls:
        n = (p.get('name') or '').lower().strip()
        out.setdefault(n, []).append(str(p.get('item_id')))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--month', required=True, help='YYYY-MM')
    p.add_argument('--dj', help='Filter DJ (partial match)')
    p.add_argument('--playlist', help='Override playlist name')
    p.add_argument('--delay', type=float, default=0.1)
    p.add_argument('--ma-host', default='192.168.214.159', help='Music Assistant API host (fix F)')
    p.add_argument('--dry-run', action='store_true',
                   help='Scrape + search + report only; do NOT create playlists')
    p.add_argument('--resume', action='store_true',
                   help='Skip DJ shows whose canonical playlist already exists in MA (fix A)')
    p.add_argument('--fill', action='store_true',
                   help='Re-search MISSING tracks into EXISTING playlists (no dupes, liked tracks untouched)')
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
        print(f"❌ Invalid month '{args.month}' — expected YYYY-MM (MM 01-12, zero-padded)")
        sys.exit(1)
    mn = mplib.MONTHS[m_int]

    print(f"\n{'═'*55}")
    print(f"  Dandelion Radio → Music Assistant")
    print(f"  {mn} {y}  | Filter: {args.dj or 'All DJs'}  | MA: {mplib.MA_HOST}")
    mode = 'DRY-RUN' if args.dry_run else ('FILL' if args.fill else 'LIVE')
    print(f"  {mode}{' | RESUME' if args.resume and not args.fill else ''}")
    print(f"{'═'*55}\n")

    print("📡 Scraping...")
    sections = scrape_month(args.month)
    if not sections:
        print("❌ No sections"); sys.exit(1)

    print(f"\n  Found {len(sections)} DJ shows:")
    for dj, tr in sorted(sections.items()):
        m_ = ' ▶' if (not args.dj or args.dj.lower() in dj.lower()) else '  '
        print(f"   {m_} {dj}: {len(tr)} tracks")

    if args.dj:
        sections = {k: v for k, v in sections.items() if args.dj.lower() in k.lower()}
        if not sections:
            print(f"❌ No match"); sys.exit(1)

    # ---- FILL MODE: re-search missing tracks into EXISTING playlists ----
    if args.fill:
        print(f"\n🔁 FILL MODE — re-searching missing tracks into EXISTING playlists")
        pl_map = get_playlist_map(token)
        if pl_map is None:
            sys.exit(2)
        t_fill = time.time()
        filled = []
        for dj, tracks in sections.items():
            base = f"dandelion radio - {mn} {y} - {dj}".lower().strip()
            ids = [i for name, idl in pl_map.items()
                   for i in idl if name == base or name.startswith(base + ' (')]
            if not ids:
                print(f"\n  ⏭️  {dj}: no existing playlist — use LIVE mode to create it")
                continue
            pid = ids[0]
            present = mplib.playlist_track_keys(token, pid)
            if present is None:
                print(f"    ❌ Could not read existing tracks for id={pid} — aborting fill (avoid duplicate adds)")
                sys.exit(2)
            missing = [t for t in tracks if mplib.norm_key(t['artist'], t['title']) not in present]
            print(f"\n  {dj} (id={pid}): {len(tracks)} expected, "
                  f"{len(tracks) - len(missing)} present, {len(missing)} missing")
            if not missing:
                continue
            new_uris, new_hits = [], []
            for i, t in enumerate(missing):
                if i % 10 == 0:
                    print(f"  [hb {time.time()-t_fill:6.0f}s] {dj[:28]:28s} "
                          f"{i}/{len(missing)} missing", flush=True)
                m = mplib.search_ma(token, t['artist'], t['title'])
                if m:
                    new_uris.append(m[0])
                    new_hits.append(f"✅ {t['artist'][:18]:18s} - {t['title'][:35]:35s} ({m[1]})")
                time.sleep(args.delay)
            print(f"  → found {len(new_hits)}/{len(missing)}")
            for line in new_hits[:10]:
                print(f"    {line}")
            if len(new_hits) > 10:
                print(f"    ... +{len(new_hits)-10} more")
            if args.dry_run:
                print(f"  (dry-run) would add {len(new_uris)} tracks to id={pid}")
            elif new_uris:
                mplib.add_to_existing(token, pid, new_uris)
                print(f"    ✅ added {len(new_uris)} to id={pid}")
                filled.append((dj, pid, len(new_uris)))
        print(f"\n{'═'*55}")
        print(f"  FILL COMPLETE — {len(filled)} playlists topped up")
        for dj, pid, n in filled:
            print(f"  ✅ {dj} (id={pid}): +{n} tracks")
        print(f"  Search errors: {mplib.SEARCH_ERRORS} (timeouts: {mplib.SEARCH_TIMEOUTS}) | Elapsed: {time.time()-t_fill:.0f}s")
        print(f"  Existing playlists & liked tracks untouched (adds only).")
        print(f"{'═'*55}\n")
        return

    print(f"\n🔍 Searching MA...")
    total = sum(len(v) for v in sections.values())
    found = 0
    res = {}
    t0 = time.time()

    existing_names = None
    if args.resume or not args.dry_run:
        print("  Checking existing playlist names...")
        existing_names = mplib.get_existing_playlist_names(token)
        if existing_names is None:
            sys.exit(2)   # fix C: fail-loud instead of creating duplicates

    for dj, tracks in sections.items():
        canonical = f"dandelion radio - {mn} {y} - {dj}".lower()
        if args.resume and existing_names and canonical in existing_names:
            print(f"\n  ⏭️  {dj}: playlist exists — skipping (--resume)")
            continue
        res[dj] = {'uris': [], 'found': [], 'missing': []}
        dj_t0 = time.time()
        for i, t in enumerate(tracks):
            # fix A: heartbeat every 10 tracks (newline, not \r — journald-safe)
            if i % 10 == 0:
                el = time.time() - t0
                print(f"  [hb {el:6.0f}s] {dj[:28]:28s} {i}/{len(tracks)} "
                      f"({found}/{total} found overall)", flush=True)
            match = mplib.search_ma(token, t['artist'], t['title'])
            if match:
                res[dj]['uris'].append(match[0])
                res[dj]['found'].append(f"✅ {t['artist'][:18]:18s} - {t['title'][:35]:35s} ({match[1]})")
                found += 1
            else:
                res[dj]['missing'].append(f"❌ {t['artist'][:18]:18s} - {t['title']}")
            time.sleep(args.delay)
        print(f"  [dj {time.time()-dj_t0:6.0f}s] {dj}: {len(res[dj]['found'])}/{len(tracks)}")

    print(f"\n\n  ✅ {found}/{total} found")
    for dj, r in res.items():
        print(f"  📋 {dj}: {len(r['found'])}/{len(r['found'])+len(r['missing'])}")
        for line in r['found'][:15]:
            print(f"    {line}")
        if len(r['found']) > 15:
            print(f"    ... +{len(r['found'])-15} more")
        for line in r['missing']:
            print(f"    {line}")

    if args.dry_run:
        print(f"\n{'═'*55}")
        print(f"  DRY RUN COMPLETE — {found}/{total} would be matched.")
        print(f"  Search errors: {mplib.SEARCH_ERRORS} (timeouts: {mplib.SEARCH_TIMEOUTS}) | Elapsed: {time.time()-t0:.0f}s")
        print(f"  No playlists created.")
        print(f"{'═'*55}\n")
        return

    print(f"🎵 Creating playlists...")
    created_this_run = set()
    created = []
    create_failures = 0
    for dj, r in res.items():
        if not r['uris']:
            continue
        if args.playlist:
            pname = args.playlist
        else:
            base_name = f"Dandelion Radio - {mn} {y} - {dj}"
            pname = base_name
            counter = 2
            while pname.lower().strip() in existing_names or pname.lower().strip() in created_this_run:
                pname = f"{base_name} ({counter})"
                counter += 1
            created_this_run.add(pname.lower().strip())
        print(f"  '{pname}' ({len(r['uris'])} tracks)...")
        pid = mplib.create_playlist(token, pname, r['uris'])
        if pid:
            created.append((pname, len(r['uris']), pid))
            print(f"    ✅ ID={pid}")
            ok = mplib.wait_for_add_tasks(token, pname)          # fix B
            if not ok:
                print(f"    ⚠️  Add tasks still pending after wait — tracks may land later")
            actual = mplib.verify_playlist_count(token, pid)   # fix E
            if actual >= 0:
                mark = "✅" if actual == len(r['uris']) else "⚠️"
                print(f"    {mark} verify: {actual}/{len(r['uris'])} tracks on playlist")
            else:
                print(f"    ⚠️  verify: could not query track count")
        else:
            create_failures += 1
            print(f"    ❌ Failed")

    if create_failures:
        print(f"  ❌ {create_failures} playlist creation(s) FAILED — exiting non-zero (L7)")
        sys.exit(2)

    print(f"\n{'═'*55}")
    for n, c, pid in created:
        print(f"  ✅ {n} ({c} tracks, id={pid})")
    print(f"  Found: {found}/{total}")
    print(f"  Search errors: {mplib.SEARCH_ERRORS} (timeouts: {mplib.SEARCH_TIMEOUTS}; each timeout retried once before marking missing)")
    all_miss = [m for r in res.values() for m in r['missing']]
    if all_miss:
        print(f"  ❌ Missing ({len(all_miss)}):")
        for m in all_miss[:10]:
            print(f"    {m}")
        if len(all_miss) > 10:
            print(f"    ... and {len(all_miss)-10} more")
    print(f"  Elapsed: {time.time()-t0:.0f}s")
    print(f"{'═'*55}\n")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except mplib.MAMidRunOutage as e:
        print(f"\n❌ ABORTED: {e}", file=sys.stderr)
        sys.exit(3)
