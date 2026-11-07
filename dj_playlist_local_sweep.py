#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""Swap provider-pinned DJ-playlist entries for local-backed library merges.

Every Dandelion/KEXP DJ playlist built before 7 Nov 2026 carries the raw
provider URIs _pick_best returned (deezer--xxx://, spotify--xxx://, ...).
Where the library holds the same recording as a MERGED item with a local
file mapping, the playlist entry is swapped to that library:// URI. MA
resolves the merge to the local file on ingest, so the entry then plays
Paul's own copy and survives any single provider dying.

Order-safe: a playlist is rebuilt IN PLACE (remove-all by position, verify
empty, re-add the corrected full URI list in original order) — the pattern
proven by kexp-rebuild-months.py. A snapshot of the original URI list is
written before any mutation, plus a per-playlist result report afterwards.

HARD LESSONS from the 8 Nov 2026 pilot (playlist 1551, now whole again):
- Provider metadata ROTS: a Bandcamp URI addable in July 2026 failed in
  Nov 2026 with `Field "duration" ... invalid value None`, and the record
  500s on direct get. Adds therefore go in small batches; a shortfall is
  bisected to singles and each poison item is routed around with a fresh
  search_ma lookup (same song, working URI) — never retried blindly.
- MA normalizes library:// merge URIs to the local file mapping on ingest,
  so post-add URI-set comparison against submitted URIs ALWAYS mismatches.
  Verification is by COUNT + song-identity sequence, never URI equality.
- NEVER auto-teardown ("restore") on a verify wobble: the original URIs
  may themselves be un-addable now, so restore can destroy what the rebuild
  just saved. Complete forward, report precisely, snapshot for humans.

Usage:
  dj_playlist_local_sweep.py [--dry-run] [--apply] [--playlist SUB]
                             [--delay SEC] [--limit N] [--include-local]
  Default is --dry-run (read-only). --apply mutates one playlist at a time.
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ma_playlist_lib as mplib

SNAP_DIR = os.environ.get("MA_SWEEP_SNAP_DIR",
                          "/root/.hermes/data/playlist-sweep-snapshots")
INCLUDE_LOCAL = False
ADD_BATCH = 25
REMOVE_CALL_CAP = 130


def list_dj_playlists(token, pattern=""):
    """All library playlists whose name matches dandelion|kexp (+filter)."""
    pls = mplib._api(token, {"message_id": "l", "command": "music/playlists/library_items",
                             "args": {"limit": 500, "offset": 0}}, timeout=120)
    if not isinstance(pls, list):
        return None
    try:
        p2 = mplib._api(token, {"message_id": "l2", "command": "music/playlists/library_items",
                                "args": {"limit": 500, "offset": 500}}, timeout=120)
        if isinstance(p2, list):
            pls = pls + p2
    except Exception:
        pass
    out = [p for p in pls
           if re.search(r"dandelion|kexp", p.get("name") or "", re.I)]
    if pattern:
        out = [p for p in out if pattern.lower() in (p.get("name") or "").lower()]
    return sorted(out, key=lambda p: p.get("name") or "")


def fetch_tracks(token, pid):
    try:
        tr = mplib._api(token, {"message_id": "t", "command": "music/playlists/playlist_tracks",
                                "args": {"item_id": str(pid),
                                         "provider_instance_id_or_domain": "library"}},
                        timeout=60)
    except Exception:
        return None
    return tr if isinstance(tr, list) else None


def artists_str(t):
    return ", ".join(a.get("name", "?") for a in (t.get("artists") or []))


def plan_playlist(token, pid, delay):
    """Return (tracks, swaps, stats).

    swaps: [(pos, old_uri, new_uri, name, artists)]. artists travel along
    so a poison item can be re-searched fresh at apply time.
    """
    tracks = fetch_tracks(token, pid)
    if tracks is None:
        return None, None, {"error": "fetch failed"}
    tracks = sorted(tracks, key=lambda t: t.get("position", 0))
    swaps, stats = [], {"total": len(tracks), "merged": 0, "local_direct": 0,
                        "pinned": 0,
                        "swappable": 0, "dup_skip": 0, "unresolved": 0}
    have_uris = {t.get("uri", "") for t in tracks}
    for t in tracks:
        uri = t.get("uri", "")
        if uri.startswith("library://"):
            stats["merged"] += 1
            continue
        if uri.startswith("filesystem_smb"):
            # 8 Nov 2026: direct-local entries already play the local file;
            # the merged target holds the identical path, so swapping only
            # churns a working entry. Keep (unless --include-local).
            if not INCLUDE_LOCAL:
                stats["local_direct"] += 1
                continue
        stats["pinned"] += 1
        try:
            new = mplib._resolve_to_local(token, t)
        except Exception:
            new = None
        time.sleep(delay)
        if not new or not new[0].startswith("library://"):
            stats["unresolved"] += 1
            continue
        if new[0] in have_uris:
            stats["dup_skip"] += 1
            continue
        swaps.append((t.get("position", 0), uri, new[0],
                      t.get("name", "?"), artists_str(t)))
        have_uris.add(new[0])
        stats["swappable"] += 1
    return tracks, swaps, stats


def snapshot(pid, name, tracks):
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, "pl-%s.json" % pid)
    with open(path + ".tmp", "w") as f:
        json.dump({"playlist_id": pid, "name": name, "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "uris": [t.get("uri", "") for t in
                            sorted(tracks, key=lambda t: t.get("position", 0))],
                   "songs": [{"pos": t.get("position", 0), "artist": artists_str(t),
                              "title": t.get("name", "?"), "uri": t.get("uri", "")}
                             for t in sorted(tracks, key=lambda t: t.get("position", 0))]}, f)
    os.replace(path + ".tmp", path)
    return path


def stable_tracks(token, pid, tries=6, pause=20):
    """Poll playlist_tracks until two consecutive reads agree. Never teardown."""
    last = None
    for _ in range(tries):
        cur = fetch_tracks(token, pid)
        if cur is not None and last is not None and len(cur) == len(last):
            return cur
        last = cur
        time.sleep(pause)
    return last if last is not None else fetch_tracks(token, pid)


def submit_add(token, pid, uris):
    """Submit one add batch (contract: db_playlist_id + uris). Returns bool."""
    try:
        mplib._api(token, {"message_id": "a", "command": "music/playlists/add_playlist_tracks",
                           "args": {"db_playlist_id": int(pid), "uris": list(uris)}}, timeout=60)
        return True
    except Exception as e:
        print(f"    ⚠️  add submit failed: {e}")
        return False


def submit_remove(token, pid, positions):
    """Submit one remove call (contract: positions_to_remove, DESCENDING)."""
    try:
        mplib._api(token, {"message_id": "r", "command": "music/playlists/remove_playlist_tracks",
                           "args": {"db_playlist_id": int(pid),
                                    "positions_to_remove": sorted(positions, reverse=True)}},
                   timeout=60)
        return True
    except Exception as e:
        print(f"    ⚠️  remove submit failed: {e}")
        return False


def serial_remove_all(token, pid, name, positions):
    """Remove positions in SERIALIZED calls of <=REMOVE_CALL_CAP.

    8 Nov 2026: firing many remove_playlist_tracks calls back-to-back lets
    MA run them concurrently against shifting positions — later tasks are
    silently dropped and the playlist is left half-emptied (exact multiples
    of the batch size missing, head-first). One call at a time with a
    count-verified settle between calls is the only safe shape.
    Returns (ok, remaining_count). Never proceeds past a stall.
    """
    positions = sorted(positions, reverse=True)
    while positions:
        chunk, positions = positions[:REMOVE_CALL_CAP], positions[REMOVE_CALL_CAP:]
        want = len(positions)  # remainder expected after this chunk lands
        if not submit_remove(token, pid, chunk):
            return False, -1
        mplib.wait_for_add_tasks(token, name, max_wait=600)
        cur = stable_tracks(token, pid, tries=9, pause=20)
        n = len(cur) if cur is not None else -1
        if n != want:
            print(f"    ⚠️  remove chunk stalled: have {n}, want {want} — STOPPING, no further calls")
            return False, n
    return True, 0


def add_until(token, pid, name, uris, want_total):
    """Submit uris, settle, return stable count. No teardown, ever."""
    submit_add(token, pid, uris)
    time.sleep(3.0)
    mplib.wait_for_add_tasks(token, name, max_wait=600)
    time.sleep(10)
    cur = stable_tracks(token, pid, tries=4, pause=15)
    return len(cur) if cur is not None else -1


def batched_add_all(token, pid, name, items):
    """Add (uri, title, artist) items in order, 25/batch, poison-bisecting.

    Returns (final_count, substitutions, omissions). Completes forward:
    poison items are routed around with a fresh lookup of the same song,
    anything still un-addable is recorded as an omission. No teardown.
    """
    substitutions, omissions = [], []
    added = len(fetch_tracks(token, pid) or [])
    i = 0
    while i < len(items):
        chunk = items[i:i + ADD_BATCH]
        uris = [c[0] for c in chunk]
        n = add_until(token, pid, name, uris, added + len(chunk))
        if n == added + len(chunk):
            added = n
            i += len(chunk)
            continue
        # Shortfall: bisect chunk into singles.
        print(f"  ⚠️  batch shortfall (want {added + len(chunk)}, have {n}) — bisecting")
        for uri, title, artist in chunk:
            before = len(fetch_tracks(token, pid) or [])
            n1 = add_until(token, pid, name + "-single", [uri], before + 1)
            if n1 == before + 1:
                added = n1
                continue
            print(f"    ☠️  poison: {artist} - {title} [{uri[:60]}] — fresh lookup")
            rep = fresh_replacement(token, artist, title)
            if rep and rep != uri:
                n2 = add_until(token, pid, name + "-single", [rep], before + 1)
                if n2 == before + 1:
                    added = n2
                    substitutions.append({"song": "%s - %s" % (artist, title),
                                          "old": uri, "new": rep})
                    print(f"    ✅ routed around: -> {rep[:70]}")
                    continue
            omissions.append({"song": "%s - %s" % (artist, title), "uri": uri})
            print("    ❌ omission recorded (song kept out, everything else continues)")
        added = len(fetch_tracks(token, pid) or [])
        i += len(chunk)
    final = stable_tracks(token, pid)
    return (len(final) if final is not None else -1), substitutions, omissions


def fresh_replacement(token, artist, title, delay=0.3):
    """Fresh search_ma for a poison item's song. Returns uri or None."""
    try:
        r = mplib.search_ma(token, artist, title)
        time.sleep(delay)
        return r[0] if r else None
    except Exception:
        return None


def apply_playlist(token, pid, name, tracks, swaps, delay):
    """Rebuild in place, completing forward. Returns (ok, msg, report).

    NEVER tears down on a wobble: original URIs may themselves be rotted
    (1551 pilot), so "restore" can destroy what the rebuild just saved.
    Removes are SERIALIZED (serial_remove_all); adds are batched with
    poison-bisect (batched_add_all). Anything still un-addable is recorded
    as an omission and reported.
    """
    by_pos = {s[0]: (s[2], s[3], s[4]) for s in swaps}  # pos -> (new_uri, title, artists)
    order = sorted(tracks, key=lambda t: t.get("position", 0))
    corrected = [by_pos[t.get("position", 0)][0] if t.get("position", 0) in by_pos
                 else t.get("uri", "") for t in order]
    plan_names = [by_pos[t.get("position", 0)][1] if t.get("position", 0) in by_pos
                  else t.get("name", "?") for t in order]
    plan_artists = [by_pos[t.get("position", 0)][2] if t.get("position", 0) in by_pos
                    else artists_str(t) for t in order]
    snap = snapshot(pid, name, tracks)
    print(f"  snapshot: {snap}")

    report = {"playlist_id": pid, "name": name, "snapshot": snap,
              "swaps_planned": len(swaps), "substitutions": [], "omissions": [],
              "final_count": 0, "final_expected": len(corrected)}

    positions = [t.get("position", 0) for t in tracks]
    ok_rm, left_n = serial_remove_all(token, pid, name, positions)
    if not ok_rm:
        return False, ("remove stalled (%s left) — STOPPED, snapshot kept, "
                       "manual call needed" % left_n), report

    items = list(zip(corrected, plan_names, plan_artists))
    final_n, subs, omis = batched_add_all(token, pid, name, items)
    report["substitutions"] = subs
    report["omissions"] = omis
    report["final_count"] = final_n
    if final_n != len(items) - len(omis):
        return False, ("count %d vs %d expected — investigate (snapshot kept, "
                       "no teardown performed)" % (final_n, len(items))), report
    # Song-identity audit: warn only, never fail.
    final = stable_tracks(token, pid, tries=3, pause=10)
    if final is not None:
        exp_keys = [mplib.norm_key(a, t) for a, t in zip(plan_artists, plan_names)]
        got = sorted(final, key=lambda t: t.get("position", 0))
        seq_ok = True
        for idx, (ek, t) in enumerate(zip(exp_keys, got)):
            gk = mplib.norm_key(artists_str(t), t.get("name", "?"))
            if ek.split("|")[0] and gk.split("|")[0] != ek.split("|")[0]:
                seq_ok = False
                print(f"    ⚠️  pos {idx + 1} artist drift: expected {plan_artists[idx]!r}, "
                      f"got {artists_str(t)!r}")
    else:
        seq_ok = True
    rpath = os.path.join(SNAP_DIR, "pl-%s-report.json" % pid)
    with open(rpath + ".tmp", "w") as f:
        json.dump(report, f)
    os.replace(rpath + ".tmp", rpath)
    msg = ("rebuilt: %d tracks%s%s" %
           (final_n,
            " (%d swaps)" % len(swaps) if not subs else
            " (%d swaps, %d poison routed)" % (len(swaps), len(subs)),
            "" if seq_ok else " — artist drift noted above"))
    if omis:
        msg += " — OMISSIONS: %d (see report)" % len(omis)
        return False, msg, report
    return True, msg, report


def recover_playlist(token, pid, name):
    """Restore a damaged playlist to its snapshot originals. Returns (ok, msg).

    Only acts when live count is STRICTLY below the snapshot count (tracks
    went missing). Wipes the scrambled remainder with serialized removes,
    then re-adds snapshot songs in order with poison-bisect. Writes
    pl-<pid>-recover.json. Never touches whole playlists.
    """
    snap_path = os.path.join(SNAP_DIR, "pl-%s.json" % pid)
    try:
        with open(snap_path) as f:
            snap = json.load(f)
    except Exception:
        return None, "no snapshot — skipped"
    songs = snap.get("songs", [])
    live = fetch_tracks(token, pid)
    if live is None:
        return False, "live fetch failed — skipped"
    if len(live) >= len(songs):
        return True, "already whole (%d tracks) — untouched" % len(live)
    print(f"  damaged: {len(live)}/{len(songs)} present — recovering from snapshot")
    ok_rm, left_n = serial_remove_all(token, pid, name,
                                      [t.get("position", 0) for t in live])
    if not ok_rm:
        return False, "wipe stalled (%s left) — STOPPED" % left_n
    items = [(s["uri"], s["title"], s["artist"]) for s in
             sorted(songs, key=lambda s: s.get("pos", 0))]
    final_n, subs, omis = batched_add_all(token, pid, name + "-recover", items)
    rep = {"playlist_id": pid, "name": name, "snapshot": snap_path,
           "expected": len(items), "final_count": final_n,
           "substitutions": subs, "omissions": omis}
    rpath = os.path.join(SNAP_DIR, "pl-%s-recover.json" % pid)
    with open(rpath + ".tmp", "w") as f:
        json.dump(rep, f)
    os.replace(rpath + ".tmp", rpath)
    if final_n != len(items) - len(omis):
        return False, ("recovered %d/%d — investigate, no teardown" % (final_n, len(items))), rep
    msg = "recovered: %d/%d tracks%s" % (final_n, len(items),
                                         (" (%d poison routed)" % len(subs)) if subs else "")
    if omis:
        return False, msg + " — OMISSIONS: %d" % len(omis), rep
    return True, msg, rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="mutate (default: dry-run)")
    ap.add_argument("--playlist", default="", help="substring filter on playlist name")
    ap.add_argument("--delay", type=float, default=0.15, help="pause between resolve calls")
    ap.add_argument("--limit", type=int, default=0, help="max playlists to process (0=all)")
    ap.add_argument("--include-local", action="store_true",
                    help="also swap direct-local entries to their merge (default: keep)")
    ap.add_argument("--recover", action="store_true",
                    help="restore damaged playlists (live < snapshot) to snapshot "
                         "originals; no swaps. Safe to run over everything.")
    args = ap.parse_args()
    global INCLUDE_LOCAL
    INCLUDE_LOCAL = args.include_local

    token = mplib.get_ma_token()
    if not token:
        print("no MA token"); sys.exit(2)
    pls = list_dj_playlists(token, args.playlist)
    if pls is None:
        print("playlist listing failed — aborting"); sys.exit(2)
    if args.limit:
        pls = pls[:args.limit]
    print(f"{len(pls)} DJ playlists {'TO RECOVER' if args.recover else ('TO SWEEP (APPLY MODE)' if args.apply else '(dry-run)')}")

    totals = {"playlists": 0, "swappable": 0, "already_merged": 0, "rebuilt": 0,
              "substituted": 0, "omitted": 0, "failed": []}
    for p in pls:
        pid, name = p.get("item_id"), p.get("name") or "?"
        print(f"\n[{pid}] {name}")
        if args.recover:
            totals["playlists"] += 1
            r = recover_playlist(token, pid, name)
            if r[0] is None:
                print("  (no snapshot — skipped)")
                continue
            ok, msg = r[0], r[1]
            print(("  ✅ " if ok else "  ❌ ") + msg)
            if not ok:
                totals["failed"].append(name)
            else:
                totals["rebuilt"] += 1
            continue
        tracks, swaps, stats = plan_playlist(token, pid, args.delay)
        if tracks is None:
            print("  ❌ fetch failed — skipped"); totals["failed"].append(name); continue
        totals["playlists"] += 1
        totals["swappable"] += stats["swappable"]
        totals["already_merged"] += stats["merged"]
        print(f"  total={stats['total']} merged={stats['merged']} local-direct={stats['local_direct']} pinned={stats['pinned']} "
              f"swappable={stats['swappable']} dup-skip={stats['dup_skip']} no-local={stats['unresolved']}")
        for pos, old, new, tname, _a in swaps[:12]:
            print(f"    pos {pos}: {tname[:40]:40s} {old.split('://')[0]:20s} -> {new}")
        if len(swaps) > 12:
            print(f"    ... +{len(swaps) - 12} more")
        if args.apply and swaps:
            ok, msg, rep = apply_playlist(token, pid, name, tracks, swaps, args.delay)
            print(("  ✅ " if ok else "  ❌ ") + msg)
            totals["substituted"] += len(rep.get("substitutions", []))
            totals["omitted"] += len(rep.get("omissions", []))
            if ok:
                totals["rebuilt"] += 1
            else:
                totals["failed"].append(name)
        elif args.apply:
            print("  (nothing to swap)")
    print("\n═══════════════════════════════════════════")
    print(f"  playlists scanned: {totals['playlists']} | already merged: {totals['already_merged']}")
    print(f"  swappable entries: {totals['swappable']}" +
          (f" | rebuilt: {totals['rebuilt']} | poison routed: {totals['substituted']} | "
           f"omitted: {totals['omitted']}" if args.apply else " (dry-run, nothing changed)"))
    if totals["failed"]:
        print(f"  ❌ failed/skipped: {len(totals['failed'])}")
        for n in totals["failed"][:10]:
            print(f"     - {n}")


if __name__ == "__main__":
    main()
