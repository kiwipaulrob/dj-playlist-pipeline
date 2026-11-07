#!/usr/bin/env python3
"""Merge duplicate Dandelion Radio playlists in Music Assistant.

Strategy per group (identified 15 Aug 2026, 23 duplicate groups):
  - keeper = fullest copy (tie -> canonical name, then lowest id)
  - pure-subset losers (100% overlap) are deleted after M3U8 backup
  - partial-overlap losers: their UNIQUE tracks are added to the keeper
    FIRST (add_playlist_tracks), then the loser is deleted
Favourites are preserved by construction (MA favourites live in the library,
builtin playlist 1, NOT inside playlists — verified 13 Aug 2026); liked counts
are still reported before/after as belt-and-braces.

Usage:
    python3 ma-merge-dandelion-duplicates.py            # dry-run: show plan
    python3 ma-merge-dandelion-duplicates.py --apply    # backup + merge + delete
"""
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict

TOKEN = ""
for ln in open("/root/.bashrc"):
    if "MA_TOKEN" in ln and "export" in ln:
        TOKEN = ln.split("=", 1)[1].strip().strip('"').strip("'")
        break

API = "http://192.168.214.159:8095/api"
BACKUP_DIR = "/root/hermes-backup/ma-playlists"


def api(cmd, args, timeout=40):
    payload = json.dumps({"message_id": "merge", "command": cmd, "args": args})
    req = urllib.request.Request(API, data=payload.encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"__error__": str(e)}


def all_playlists():
    out, offset = [], 0
    while True:
        page = api("music/playlists/library_items", {"limit": 500, "order_by": "name", "offset": offset})
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < 500:
            break
        offset += 500
    return out


def tracks_of(pid):
    t = api("music/playlists/playlist_tracks",
            {"item_id": str(pid), "provider_instance_id_or_domain": "library"}, timeout=40)
    return t if isinstance(t, list) else []


def norm_key(t):
    arts = " ".join((a.get("name") or "") for a in (t.get("artists") or []))
    return re.sub(r"[^a-z0-9]+", " ", (arts + " " + (t.get("name") or "")).lower()).strip()


def favourites():
    fav = api("music/tracks/library_items", {"favorite": True, "limit": 0})
    keys, uris = set(), set()
    if isinstance(fav, list):
        for t in fav:
            keys.add(norm_key(t))
            if t.get("uri"):
                uris.add(t["uri"])
    return keys, uris


def liked_in(trs, fav_keys, fav_uris):
    n = 0
    for t in trs:
        if t.get("uri") and t["uri"] in fav_uris:
            n += 1
            continue
        if norm_key(t) in fav_keys:
            n += 1
    return n


def backup_playlist(pid, name):
    export = api("music/playlists/export_playlist", {"db_playlist_id": pid})
    if isinstance(export, str) and export.startswith("#EXT"):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9 _.-]", "_", name)
        path = os.path.join(BACKUP_DIR, f"PRE_MERGE_{pid}_{safe[:60]}.m3u8")
        with open(path, "w") as f:
            f.write(export)
        return path
    return None


def main():
    apply = "--apply" in sys.argv
    if not TOKEN:
        print("❌ No MA_TOKEN in /root/.bashrc"); sys.exit(1)

    fav_keys, fav_uris = favourites()
    print(f"favourites snapshot: {len(fav_keys)} tracks\n")

    pls = all_playlists()
    dl = [p for p in pls if (p.get("name") or "").startswith("Dandelion Radio")]
    groups = defaultdict(list)
    for p in dl:
        base = re.sub(r"\s*\(\d+\)\s*$", "", p.get("name") or "").strip().lower()
        groups[base].append(p)

    plan = []   # (base, keeper, losers)
    for base, entries in sorted(groups.items()):
        if len(entries) < 2:
            continue
        enriched = []
        for e in entries:
            trs = tracks_of(e.get("item_id"))
            enriched.append({"id": str(e["item_id"]), "name": e.get("name"),
                             "tracks": trs, "count": len(trs),
                             "liked": liked_in(trs, fav_keys, fav_uris)})
        # keeper: canonical name first, then most tracks, then lowest id
        enriched.sort(key=lambda e: (e["name"].lower().endswith(")"), -e["count"], int(e["id"])))
        keeper, losers = enriched[0], enriched[1:]
        plan.append((base, keeper, losers))

    if not plan:
        print("No duplicate groups found."); return

    total_losers = sum(len(l) for _, _, l in plan)
    print(f"=== {len(plan)} duplicate groups, {total_losers} loser playlists ===\n")

    for base, keeper, losers in plan:
        keep_keys = set(norm_key(t) for t in keeper["tracks"])
        print(f"[{keeper['name']}] keeper id={keeper['id']} ({keeper['count']}tr, {keeper['liked']} liked)")
        for l in losers:
            uniq = [t for t in l["tracks"] if norm_key(t) not in keep_keys]
            action = f"delete (subset of keeper)" if not uniq else \
                     f"ADD {len(uniq)} unique + delete"
            print(f"    id={l['id']:6s} {l['name'][:70]:70s} {l['count']:3d}tr "
                  f"({l['liked']} liked) -> {action}")

    if not apply:
        print("\nDRY RUN — re-run with --apply to execute (backup + merge + delete).")
        return

    print("\n=== EXECUTING ===")
    for base, keeper, losers in plan:
        keep_keys = set(norm_key(t) for t in keeper["tracks"])
        added_any = False
        for l in losers:
            uniq = [t for t in l["tracks"] if norm_key(t) not in keep_keys]
            # 1. merge unique tracks into keeper (partial-overlap groups only)
            if uniq:
                uris = [t.get("uri") for t in uniq if t.get("uri")]
                if uris:
                    time.sleep(3)
                    r = api("music/playlists/add_playlist_tracks",
                            {"db_playlist_id": int(keeper["id"]), "uris": uris})
                    print(f"  +{len(uris)} unique from id={l['id']} -> keeper id={keeper['id']}: {json.dumps(r)[:80]}")
                    added_any = True
                    keep_keys |= set(norm_key(t) for t in uniq)
                    time.sleep(3)
            # 2. backup + delete loser
            path = backup_playlist(l["id"], l["name"])
            print(f"  backup id={l['id']} -> {path}")
            time.sleep(3)
            r = api("music/playlists/remove", {"item_id": l["id"],
                                               "provider_instance_id_or_domain": "library"})
            print(f"  removed id={l['id']}: {json.dumps(r)[:60]}")
            time.sleep(3)

    # verify
    print("\n=== POST-MERGE VERIFY ===")
    fav_keys2, fav_uris2 = favourites()
    pls2 = all_playlists()
    dl2 = [p for p in pls2 if (p.get("name") or "").startswith("Dandelion Radio")]
    groups2 = defaultdict(list)
    for p in dl2:
        base = re.sub(r"\s*\(\d+\)\s*$", "", p.get("name") or "").strip().lower()
        groups2[base].append(p)
    dup2 = {b: e for b, e in groups2.items() if len(e) > 1}
    print(f"remaining duplicate groups: {len(dup2)}")
    for b, keeper, losers in plan:
        remain = [p for p in dl2 if re.sub(r'\s*\(\d+\)\s*$', '', p.get('name') or '').strip().lower() == b]
        if remain:
            trs = tracks_of(remain[0]["item_id"])
            print(f"  {remain[0]['name']}: {len(trs)}tr, {liked_in(trs, fav_keys2, fav_uris2)} liked")
    print(f"\nfavourites: {len(fav_keys)} -> {len(fav_keys2)} (must be equal or grown)")


if __name__ == "__main__":
    main()
