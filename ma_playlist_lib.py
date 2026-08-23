#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""Shared Music Assistant plumbing for the station→MA playlist scripts.

Consolidated 15 Aug 2026 (L8): dandelion-to-ma.py and kexp-to-ma.py were each
carrying ~200 lines of identical MA plumbing that had already drifted once
(the kexp M2 gap was the proof). This module is the single source of truth
for the MA side of the pipeline:

  - token resolution + JSON-RPC call (_api, get_ma_token)
  - provider-priority search (search_ma / _pick_best / _provider_priority)
  - paginated playlist listing (fetch_all_playlists) — MA caps library_items
    at 500/page (verified live 15 Aug 2026: 1009 playlists, page 1 = exactly
    500); single-page fetches silently blind duplicate/resume protection
  - playlist create/add/verify/wait (create_playlist, add_to_existing,
    verify_playlist_count, wait_for_add_tasks)
  - fill-mode presence check (playlist_track_keys, norm_key)

FAIL-LOUD CONTRACT (load-bearing, do NOT weaken): every API failure surfaces
as None — never a silent empty set/list — so callers abort with exit 2 instead
of creating duplicate playlists or re-adding whole shows. The June 2026
20-duplicate incident was caused by exactly this class of silent empty set.

Set MA_HOST before calling anything (defaults to the home MA server); the
station scripts override it from their --ma-host flag.
"""
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

MA_HOST = "192.168.214.159"

MONTHS = ['', 'January', 'February', 'March', 'April', 'May', 'June',
          'July', 'August', 'September', 'October', 'November', 'December']

# ---- unavailable-tracks store (feature 1, 23 Aug 2026) ----------------------
# Durable record of radio-tracklist songs that NEVER matched any MA provider
# at build time. Previously the missing list was console-printed once and lost
# when the run exited — the dashboard could only see provider chips for tracks
# that made it INTO playlists, never the ones left behind. Store shape:
#   { "<station>|<month>|<dj>|<norm_key>": {
#       artist, title, station, month, dj,
#       reason: "no_match" (permanent) | "timeout" | "api_error" (retryable),
#       first_seen, last_seen, attempts } }
# Reconciliation: fill mode CLEARS an entry when the track is finally found,
# so the store always reflects "still unavailable as of last touch".
UNAVAILABLE_FILE = os.environ.get("MA_UNAVAILABLE_FILE",
                                  "/root/.hermes/data/unavailable-tracks.json")
_unavail_lock = threading.Lock()

# Why the MOST RECENT search_ma() call failed: None (succeeded, or genuinely
# not on any provider → "no_match"), "timeout", or "api_error". Each station
# script is single-threaded around its search loop, so reading this right
# after search_ma() returns is race-free.
LAST_SEARCH_REASON = None


def load_unavailable():
    """Whole store as dict (empty on missing file; corrupt file reads as empty)."""
    try:
        with open(UNAVAILABLE_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"    ⚠️  unavailable-store unreadable ({e}) — treating as empty")
        return {}


def _save_unavailable(store):
    os.makedirs(os.path.dirname(UNAVAILABLE_FILE), exist_ok=True)
    tmp = UNAVAILABLE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=1, sort_keys=True)
    os.replace(tmp, UNAVAILABLE_FILE)   # atomic — readers never see partial


def _modify_unavailable(fn):
    """Exclusive CROSS-PROCESS read-modify-write of the store.

    Two station runs CAN overlap (the dashboard caps concurrency at 2 but a
    manual CLI run may still collide). Plain last-writer-wins would silently
    drop one run's records; flock serialises read-modify-write cycles.
    """
    import fcntl
    lock_path = UNAVAILABLE_FILE + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with _unavail_lock:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                store = load_unavailable()
                fn(store)
                _save_unavailable(store)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def record_unavailable_many(station, month, entries):
    """Bulk-upsert unmatched tracks. entries: [(dj, artist, title, reason)].

    Re-recording an existing key bumps attempts/last_seen; a retryable reason
    on top of a permanent one upgrades it (most recent evidence wins).
    """
    if not entries:
        return
    now = time.strftime("%Y-%m-%d %H:%M:%S")

    def mod(s):
        for dj, artist, title, reason in entries:
            key = f"{station}|{month}|{dj}|{norm_key(artist, title)}"
            e = s.get(key)
            if e:
                e["last_seen"] = now
                e["attempts"] = int(e.get("attempts", 0)) + 1
                if reason != "no_match":
                    e["reason"] = reason
            else:
                s[key] = {"artist": artist, "title": title, "station": station,
                          "month": month, "dj": dj, "reason": reason,
                          "first_seen": now, "last_seen": now, "attempts": 1}
    try:
        _modify_unavailable(mod)
    except Exception as e:
        print(f"    ⚠️  could not record unavailable tracks ({e})")


def record_unavailable(station, month, dj, artist, title, reason):
    """Single-track convenience wrapper."""
    record_unavailable_many(station, month, [(dj, artist, title, reason)])


def clear_unavailable_many(station, month, items):
    """Bulk-remove entries whose tracks were FOUND on a later fill/run.
    items: [(dj, artist, title)] — unknown keys are silently ignored."""
    if not items:
        return

    def mod(s):
        for dj, artist, title in items:
            s.pop(f"{station}|{month}|{dj}|{norm_key(artist, title)}", None)
    try:
        _modify_unavailable(mod)
    except Exception as e:
        print(f"    ⚠️  could not clear unavailable entries ({e})")


def clear_unavailable(station, month, dj, artist, title):
    """Single-track convenience wrapper."""
    clear_unavailable_many(station, month, [(dj, artist, title)])


def get_ma_token():
    t = os.environ.get('MA_TOKEN', '')
    if not t:
        for ln in open('/root/.bashrc'):
            if 'MA_TOKEN' in ln and 'export' in ln:
                t = ln.split('=', 1)[1].strip().strip('"\'').strip()
                break
    return t


def _api(token, payload, timeout=60):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"http://{MA_HOST}:8095/api", data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---- search ----------------------------------------------------------------
# L1 (15 Aug 2026): timeouts counted SEPARATELY from other errors — previously
# _do_search lumped every exception into SEARCH_TIMEOUTS, so connection
# failures/HTTP errors were reported as search timeouts.
SEARCH_TIMEOUTS = 0
SEARCH_ERRORS = 0

# Circuit breaker (22 Aug 2026): if MA dies MID-RUN (e.g. prox2 OOM kills
# VM100), every remaining search burns its 60s timeout — a ~800-track month
# would sit in timeout jail until the cron's 3600s kill. After this many
# consecutive timeouts _do_search raises MAMidRunOutage so the caller can
# abort fast with a clear error.
MAX_CONSECUTIVE_TIMEOUTS = 5
_consecutive_timeouts = 0


class MAMidRunOutage(RuntimeError):
    pass

# Provider preference for choosing WHICH copy of a matched track to use
# (user requirement 15 Aug 2026): local/share files first, then Deezer,
# then Bandcamp, then Spotify, then other providers. BBC Sounds is REMOVED
# as an option entirely — a bbc_sounds-only match is never accepted.
# NOTE: MA's music/search `provider_domains` arg is NOT a strict filter
# (verified live 15 Aug 2026 — returns other providers anyway), so the
# preference is applied as result ranking, not as a search restriction.
PROVIDER_PRIORITY = {
    "files": 0, "filesystem": 0, "filesystem_smb": 0, "library": 0,
    "deezer": 1,
    "bandcamp": 2,
    "spotify": 3,
}
PROVIDER_OTHER = 4
PROVIDER_BLOCKED = {"bbc_sounds"}


def _provider_priority(t):
    """Best (lowest) priority among a track's provider mappings.

    Returns None when EVERY mapping is a blocked provider (bbc_sounds only)
    — such a track is not an option. A track mapped to e.g. deezer + bbc_sounds
    resolves to the deezer copy (priority 1).
    """
    domains = [m.get("provider_domain", "") for m in (t.get("provider_mappings") or [])]
    if not domains:
        return PROVIDER_OTHER
    prios = [PROVIDER_PRIORITY.get(d, PROVIDER_OTHER) for d in domains
             if d not in PROVIDER_BLOCKED]
    if not prios:
        return None
    return min(prios)


def _is_timeout(e):
    """True when an exception is a request timeout (possibly nested in URLError)."""
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    if isinstance(e, urllib.error.URLError):
        return _is_timeout(e.reason)
    return False


def _do_search(token, payload):
    """Run one music/search; count timeouts vs other errors separately (L1).

    Raises MAMidRunOutage after MAX_CONSECUTIVE_TIMEOUTS consecutive request
    timeouts — MA is almost certainly down mid-run; grinding on is pointless.
    """
    global SEARCH_TIMEOUTS, SEARCH_ERRORS, _consecutive_timeouts
    try:
        result = _api(token, payload, timeout=60)
        _consecutive_timeouts = 0
        return result
    except Exception as e:
        if _is_timeout(e):
            SEARCH_TIMEOUTS += 1
            _consecutive_timeouts += 1
            if _consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                raise MAMidRunOutage(
                    f"{_consecutive_timeouts} consecutive MA request timeouts — "
                    f"MA ({MA_HOST}:8095) appears to be down; aborting run"
                ) from e
        else:
            SEARCH_ERRORS += 1
            _consecutive_timeouts = 0
        return None


def _query_strategies(artist, title):
    """Query variants, best-first.

    Full credit lists ("A, B, C, D") poison MA's search — the comma-joined
    artist string buries the title and returns noise (Aug 2026 favourites
    recovery: title-only recovered 22/47 vs 0 with the full list). For
    multi-artist credits, lead with title-only and let _pick_best's artist
    validation do the matching against ANY credited name.
    """
    primary = artist.split(",")[0].strip()
    multi = len(re.split(r",|&|;", artist)) > 1 if artist else False
    if not artist:
        return [title]
    if multi:
        return [
            f"{primary[:40]} {title[:50]}",   # primary artist + title
            f"{title[:60]}",                  # title-only fallback
        ]
    return [f"{artist[:40]} {title[:50]}"]


def search_ma(token, artist, title):
    """Search MA for artist+title; return (uri, provider_domain, name) or None.

    Tries library-only queries first (fast), then cross-provider. Returns
    None both for "genuinely not found" AND for failed requests — callers
    distinguish via the SEARCH_ERRORS / SEARCH_TIMEOUTS counters, which is
    why those counters must stay accurate (L1).

    Feature 1 (23 Aug 2026): on a None return, LAST_SEARCH_REASON tells the
    caller WHY — None (genuinely not on any provider → record as "no_match"),
    "timeout", or "api_error" (both retryable). Callers read it immediately
    after the call; the station scripts' search loops are single-threaded.
    """
    global LAST_SEARCH_REASON
    # Delta-snapshot the run-global error counters so failure classification
    # reflects THIS call only (the counters accumulate across the whole run).
    t0_timeouts, t0_errors = SEARCH_TIMEOUTS, SEARCH_ERRORS
    # Library-only first (fast — local files & shares)
    for query in _query_strategies(artist, title):
        payload = {"message_id": "s", "command": "music/search",
                   "args": {"search_query": query[:90], "media_types": ["track"],
                            "limit": 20, "library_only": True}}
        match = _pick_best(_do_search(token, payload), artist, title)
        if match:
            LAST_SEARCH_REASON = None
            return match

    # Fall back to cross-provider search (slow — Deezer, Bandcamp, Spotify).
    # limit=20 so multiple providers' copies of the same track show up for
    # the provider-priority ranking in _pick_best.
    payload = {"message_id": "s2", "command": "music/search",
               "args": {"media_types": ["track"], "limit": 20,
                        "library_only": False}}
    for query in _query_strategies(artist, title):
        payload["args"]["search_query"] = query[:90]
        result = _do_search(token, payload)          # first attempt
        if result is None:
            time.sleep(2)                            # brief pause, then one retry
            result = _do_search(token, payload)
        match = _pick_best(result, artist, title)
        if match:
            LAST_SEARCH_REASON = None
            return match
    # Every strategy exhausted — classify WHY using this call's counter deltas:
    # timeout wins (transient MA/network trouble → worth re-running fill),
    # then api_error (same), else genuinely absent from every provider.
    if SEARCH_TIMEOUTS > t0_timeouts:
        LAST_SEARCH_REASON = "timeout"
    elif SEARCH_ERRORS > t0_errors:
        LAST_SEARCH_REASON = "api_error"
    else:
        LAST_SEARCH_REASON = "no_match"
    return None


STOPWORDS = {"the", "and", "feat", "ft", "with", "various", "artists", "artist",
             "various_artists", "unknown", "soundtrack", "original", "cast"}
# Words so generic they match half the catalogue — never used for artist validation.


def _fold(text):
    """Lowercase + strip diacritics (Beyoncé == beyonce) for word comparison."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c)).lower()


def _sig_words(name):
    """Significant artist-name words: len>2, non-stopword, diacritic-folded."""
    return {w for w in re.findall(r"[a-z0-9']+", _fold(name))
            if len(w) > 2 and w not in STOPWORDS}


def _artist_words(artist):
    """All significant words across a possibly comma-joined credit list."""
    words = set()
    for part in re.split(r",|&|;| feat\.? | ft\.? ", artist or ""):
        words |= _sig_words(part)
    return words


def _pick_best(result, artist, title):
    if not result:
        return None
    tracks = result.get('tracks', [])
    if not tracks:
        return None

    al, tl = artist.lower()[:15], title.lower()[:25]
    tw = [w for w in tl.split() if len(w) > 2][:4]
    aw = _artist_words(artist)          # significant words across ALL credited artists
    best, bs, best_prio = None, 0, None
    for t in tracks:
        s, tn = 0, t.get('name', '').lower()
        artists = t.get('artists') or []
        ta_words = set()
        for a in artists:
            ta_words |= _sig_words(a.get('name', ''))
        for w in tw:
            if w in tn:
                s += 2
        # Artist confirmation: >=1 significant shared word between any credited
        # query artist and any result artist. Title overlap ALONE is NOT enough:
        # Bandcamp/Deezer credit obscure releases to the LABEL page ("Slovenly
        # Recordings"), and pre-Aug-2026 scoring let those win on title points
        # alone (150+ mislabeled Dandelion playlist entries, diagnosed 22 Aug
        # 2026). Tracks with zero artist overlap go to the missing list instead.
        confirmed = bool(aw & ta_words)
        if aw and not confirmed:
            continue
        if confirmed:
            s += 3
        for a in artists:
            al_folded = _fold(a.get('name', ''))
            if al in al_folded or _fold(al) in al_folded:
                s += 1   # extra credit for substring-level artist agreement
                break
        if s < 2:
            continue
        prio = _provider_priority(t)
        if prio is None:
            continue   # bbc_sounds-only match — not an option (user req)
        # provider preference dominates, then match score
        if best_prio is None or prio < best_prio or (prio == best_prio and s > bs):
            best, bs, best_prio = t, s, prio
    if best:
        mappings = best.get('provider_mappings') or []
        prov = mappings[0].get('provider_domain', '') if mappings else ''
        return (best.get('uri', ''), prov, best.get('name', '?'))
    return None


# ---- existing playlists (fail-loud: None on failure, never silent set()) ----
def fetch_all_playlists(token):
    """All library playlists, paginated past the 500-per-call cap.

    MA silently caps library_items at 500 results per page (verified live
    15 Aug 2026: 1009 playlists exist, first page returns exactly 500).
    A single-page fetch silently blinds the duplicate-protection logic to
    everything past position 500. Returns the full list, or None on failure
    (fail-loud, same contract as get_existing_playlist_names).
    """
    out, offset, pages = [], 0, 0
    while True:
        try:
            page = _api(token, {"message_id": "1", "command": "music/playlists/library_items",
                                "args": {"limit": 500, "order_by": "name", "offset": offset}},
                        timeout=30)
        except Exception as e:
            print(f"    ❌ fetch_all_playlists: API call failed at offset {offset} ({e}) — aborting to avoid duplicate creation")
            return None
        if not isinstance(page, list):
            print(f"    ❌ fetch_all_playlists: expected list at offset {offset}, got {type(page).__name__} — aborting")
            return None
        out.extend(page)
        if len(page) < 500:
            break
        offset += 500
        pages += 1
        # L2 (15 Aug 2026): page cap — 200 pages = 100k playlists. If MA ever
        # ignored the offset (every page full), the old loop spun forever.
        if pages >= 200:
            print("    ❌ fetch_all_playlists: 200 full pages fetched without end — MA ignoring offset? aborting")
            return None
    return out


def get_existing_playlist_names(token):
    """Return set of lowercase playlist names, or None if the API call fails."""
    pls = fetch_all_playlists(token)
    if pls is None:
        return None
    return set(p.get('name', '').lower().strip() for p in pls)


# ---- playlist creation (wait for add tasks; verify counts) -----------------
def create_playlist(token, name, uris):
    pid = None
    try:
        pid = _api(token, {"message_id": "c", "command": "music/playlists/create_playlist",
                           "args": {"name": name}}, timeout=30).get('item_id')
    except Exception as e:
        print(f"    ❌ Create failed: {e}")
    if not pid:
        print(f"    ❌ Create returned no playlist ID")
        return None
    for i in range(0, len(uris), 25):
        batch = uris[i:i + 25]
        try:
            # L0/L6 (15 Aug 2026): int(pid) — add_to_existing already wrapped
            # int(); the create path passed the raw string item_id. Aligned.
            _api(token, {"message_id": "a", "command": "music/playlists/add_playlist_tracks",
                         "args": {"db_playlist_id": int(pid), "uris": batch}}, timeout=30)
        except Exception as e:
            print(f"    ⚠️  Add batch {i // 25 + 1} failed: {e}")
        time.sleep(3.0)      # MA write-op pacing ≥3s
    return pid


def add_to_existing(token, pid, uris):
    """Add URIs to an existing playlist (batches of 25, ≥3s pacing)."""
    for i in range(0, len(uris), 25):
        batch = uris[i:i + 25]
        try:
            _api(token, {"message_id": "a", "command": "music/playlists/add_playlist_tracks",
                         "args": {"db_playlist_id": int(pid), "uris": batch}}, timeout=30)
        except Exception as e:
            print(f"    ⚠️  Add batch {i // 25 + 1} failed: {e}")
        time.sleep(3.0)
    return True


def remove_positions_from_existing(token, pid, positions):
    """Remove tracks from an existing playlist by POSITION.

    Command contract verified 15 Aug 2026 from the MA 2.9.x source
    (music_assistant/controllers/media/playlists.py): remove_playlist_tracks
    takes ``positions_to_remove: tuple[int, ...]`` — NOT uris (an uri-based
    payload 500s every time). Positions are the item ``position`` fields from
    playlist_tracks; pass them DESCENDING (positions shift as items are
    removed). Runs as a background task server-side; callers should
    wait_for_add_tasks (the task name contains the playlist name)."""
    for i in range(0, len(positions), 25):
        batch = sorted(positions[i:i + 25], reverse=True)
        try:
            _api(token, {"message_id": "r", "command": "music/playlists/remove_playlist_tracks",
                         "args": {"db_playlist_id": int(pid), "positions_to_remove": batch}}, timeout=30)
        except Exception as e:
            print(f"    ⚠️  Remove batch {i // 25 + 1} failed: {e}")
        time.sleep(3.0)
    return True


def wait_for_add_tasks(token, playlist_name, max_wait=300):
    """Poll MA tasks until no pending/running add-task for this playlist.

    The add-task name is 'Add items to playlist <name>' (verified live
    15 Aug 2026) — the playlist-name substring match below is correct.
    """
    name_l = playlist_name.lower()
    waited = 0
    while waited < max_wait:
        try:
            tasks = _api(token, {"message_id": "tl", "command": "tasks/list", "args": {}}, timeout=20)
        except Exception:
            return False  # cannot determine — report at end
        if not isinstance(tasks, list):
            return False
        pending = [t for t in tasks
                   if name_l in (t.get('name') or '').lower()
                   and t.get('status') in ('pending', 'running')]
        if not pending:
            return True
        time.sleep(10)
        waited += 10
    return False


def verify_playlist_count(token, pid):
    """Query actual track count for a created playlist (post-create verify).

    (L5, 15 Aug 2026: the old `expected` parameter was never read — callers
    do their own comparison against len(uris).)
    """
    try:
        tracks = _api(token, {"message_id": "v", "command": "music/playlists/playlist_tracks",
                              "args": {"item_id": str(pid),
                                       "provider_instance_id_or_domain": "library"}}, timeout=30)
        return len(tracks) if isinstance(tracks, list) else -1
    except Exception:
        return -1


# ---- fill mode helpers (re-search missing tracks into existing playlists) ----
def norm_key(artist, title):
    """Normalized artist|title key for presence comparison."""
    return (re.sub(r'[^a-z0-9]+', ' ', (artist or '').lower()).strip()
            + '|' + re.sub(r'[^a-z0-9]+', ' ', (title or '').lower()).strip())


def playlist_track_keys(token, pid):
    """Return set of norm_key(artist,title) already present in a playlist.

    Returns None on API failure — callers MUST abort. An empty set() on
    failure would make fill mode believe every expected track is missing and
    re-add the whole show (duplicate explosion, same bug class as fix C).
    """
    try:
        tr = _api(token, {"message_id": "v", "command": "music/playlists/playlist_tracks",
                          "args": {"item_id": str(pid),
                                   "provider_instance_id_or_domain": "library"}}, timeout=30)
    except Exception as e:
        print(f"    ❌ playlist_track_keys: API call failed for id={pid} ({e}) — aborting to avoid duplicate adds")
        return None
    if not isinstance(tr, list):
        print(f"    ❌ playlist_track_keys: expected list for id={pid}, got {type(tr).__name__} — aborting")
        return None
    keys = set()
    for t in tr:
        arts = ' '.join((a.get('name') or '') for a in (t.get('artists') or []))
        keys.add(norm_key(arts, t.get('name') or ''))
    return keys
