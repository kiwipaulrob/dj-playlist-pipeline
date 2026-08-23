"""Dandelion dashboard — MA data layer (no external deps beyond stdlib).

Provides: MA API helper, favourites set, playlist snapshot (track counts,
liked counts), station data sources (dandelion scrape, kexp API), run registry.
"""
import json
import os
import re
import threading
import time
import urllib.request

MA_API = "http://192.168.214.159:8095/api"
RUNS_DIR = "/root/.hermes/data/dandelion-runs"
CACHE_FILE = "/root/.hermes/data/dandelion-status-cache.json"

MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]

STATIONS = {
    "dandelion": {"prefix": "Dandelion Radio", "label": "Dandelion Radio"},
    "kexp": {"prefix": "KEXP", "label": "KEXP Midday Show"},
}

# Pretty names for provider_domain values seen in provider_mappings.
PROVIDER_LABELS = {
    "bandcamp": "Bandcamp", "spotify": "Spotify", "deezer": "Deezer",
    "bbc_sounds": "BBC Sounds", "qobuz": "Qobuz", "youtube": "YouTube",
    "files": "Local", "library": "Library", "tunein": "TuneIn",
}


def provider_counts(tracks):
    """{provider_label: n} — songs per provider across a playlist's tracks.

    Each track's provider_mappings lists every provider MA has matched that
    song to; a song matched to two providers counts for both. Tracks with no
    mappings land under 'unmatched'. Sorted by count desc.
    """
    counts = {}
    for t in tracks or []:
        pms = t.get("provider_mappings") or []
        if not pms:
            counts["unmatched"] = counts.get("unmatched", 0) + 1
            continue
        for m in pms:
            d = (m.get("provider_domain") or "").strip()
            if not d:
                continue
            label = PROVIDER_LABELS.get(d, d.title())
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def get_ma_token():
    t = os.environ.get("MA_TOKEN", "")
    if not t:
        for ln in open("/root/.bashrc"):
            if "MA_TOKEN" in ln:
                t = ln.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return t


def ma_call(command, args=None, timeout=40):
    tok = get_ma_token()
    payload = json.dumps({"message_id": "dd", "command": command, "args": args or {}}).encode()
    req = urllib.request.Request(MA_API, data=payload, headers={
        "Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"__error__": str(e)}


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_FAV_CACHE = {"ts": 0, "keys": set(), "uris": set()}
FAV_TTL = 30  # build_status calls get_favourites twice per request (M5) — short cache


def get_favourites():
    """Return (set_of_norm_artist_title, set_of_uris) from MA favourites.

    Cached 30s: playlist_snapshot() and build_status() both call this on the
    same status request; each call is a 797-track library scan (verified live
    15 Aug 2026), so a short TTL removes the duplicate fetch.
    """
    now = time.time()
    if now - _FAV_CACHE["ts"] < FAV_TTL and (_FAV_CACHE["keys"] or _FAV_CACHE["uris"]):
        return _FAV_CACHE["keys"], _FAV_CACHE["uris"]
    fav = ma_call("music/tracks/library_items", {"favorite": True, "limit": 0})
    keys, uris = set(), set()
    if isinstance(fav, list):
        for t in fav:
            arts = " ".join((a.get("name") or "") for a in (t.get("artists") or []))
            keys.add(norm(arts + " " + (t.get("name") or "")))
            if t.get("uri"):
                uris.add(t["uri"])
    _FAV_CACHE.update(ts=now, keys=keys, uris=uris)
    return keys, uris


def liked_count(tracks, fav_keys, fav_uris):
    """Count how many tracks in a playlist's track list are liked."""
    n = 0
    for t in tracks or []:
        if t.get("uri") and t["uri"] in fav_uris:
            n += 1
            continue
        arts = " ".join((a.get("name") or "") for a in (t.get("artists") or []))
        if norm(arts + " " + (t.get("name") or "")) in fav_keys:
            n += 1
    return n


def all_playlists():
    """All library playlists, paginated past MA's 500-per-call cap.

    MA silently caps library_items at 500 results per page (verified live
    15 Aug 2026: 1009 playlists exist; a single page returns exactly 500).
    Single-page fetches silently blind duplicate-detection and the dashboard
    to everything past position 500. Returns a list, or [] on failure.
    """
    out, offset, pages = [], 0, 0
    while True:
        page = ma_call("music/playlists/library_items", {"limit": 500, "offset": offset}, timeout=30)
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < 500:
            break
        offset += 500
        pages += 1
        # L2 (15 Aug 2026): page cap — 200 pages = 100k playlists; a
        # pathological MA response (500 every page) can't loop forever.
        if pages >= 200:
            break
    return out


def existing_for(month, dj=None, station="dandelion"):
    """Return list of existing playlist names matching month (and optional DJ).

    month is 'YYYY-MM'; names use '<Station> - June 2026 - DJ' format.
    Used by the trigger to decide fill-vs-live automatically.
    """
    prefix = STATIONS.get(station, STATIONS["dandelion"])["prefix"]
    try:
        y, m = month.split("-")
        mon_name = MONTH_NAMES[int(m)]
    except Exception:
        return []
    want = f"{prefix} - {mon_name} {y} - "
    pls = all_playlists()
    out = []
    for p in pls:
        name = (p.get("name") or "")
        if not name.startswith(want):
            continue
        if dj and dj.lower() not in name.lower():
            continue
        out.append(name)
    return out


def months_with_playlists(station="dandelion"):
    """Set of 'YYYY-MM' months that have at least one station playlist.

    Single paginated pass over the library (same fetch playlist_snapshot
    uses). Returns set() on API failure — callers must treat that as
    "cannot determine", never as proof a month is empty.
    """
    prefix = STATIONS.get(station, STATIONS["dandelion"])["prefix"]
    pls = all_playlists()
    if not pls:
        return set()
    months = set()
    for p in pls:
        name = p.get("name", "") or ""
        if not name.startswith(prefix + " - "):
            continue
        m = re.match(rf"{re.escape(prefix)} - ([A-Za-z]+) (\d{{4}}) - ", name)
        if not m:
            continue
        try:
            mi = MONTH_NAMES.index(m.group(1))
        except ValueError:
            continue
        months.add(f"{m.group(2)}-{mi:02d}")
    return months


def playlist_snapshot(station="dandelion"):
    """Return {month: {dj_name: {id, name, tracks, liked, providers, month, year}}}."""
    prefix = STATIONS.get(station, STATIONS["dandelion"])["prefix"]
    pls = all_playlists()
    if not pls:
        return {}, {"__error__": "no playlists returned"}
    fav_keys, fav_uris = get_favourites()

    out = {}
    for p in pls:
        name = p.get("name", "") or ""
        if not name.startswith(prefix + " - "):
            continue
        m = re.match(rf"{re.escape(prefix)} - ([A-Za-z]+) (\d{{4}}) - (.+)", name)
        if not m:
            continue
        mon_name, year, dj = m.group(1), m.group(2), m.group(3)
        month = f"{year}-{MONTH_NAMES.index(mon_name):02d}"
        tr = ma_call("music/playlists/playlist_tracks",
                     {"item_id": str(p.get("item_id")),
                      "provider_instance_id_or_domain": "library"}, timeout=30)
        n_tracks = len(tr) if isinstance(tr, list) else -1
        n_liked = liked_count(tr, fav_keys, fav_uris) if isinstance(tr, list) else -1
        provs = provider_counts(tr) if isinstance(tr, list) else {}
        entry = {"id": str(p.get("item_id")), "name": name, "tracks": n_tracks,
                 "liked": n_liked, "providers": provs, "month": month,
                 "year": year, "dj": dj}
        out.setdefault(month, {})[dj] = entry
    return out, {}


# Scrapling's Fetcher shares ONE browser context process-wide: concurrent
# Fetcher.get() calls race each other's navigations and return PARTIAL DOMs.
# Observed 15 Aug 2026: four parallel month scrapes came back 14/24, 12/19,
# 10/… and 11/… sections (every month wrong, different subsets), while the
# same call made serially returns the full page. Serialize the fetch.
_SCRAPE_LOCK = threading.Lock()


def scrape_expected(month):
    """Scrape a dandelion month's tracklist page; return {dj: count}."""
    try:
        from scrapling.fetchers import Fetcher
    except ImportError:
        return {}
    url = f"https://www.dandelionradio.com/tracklists/{month}/main.htm"
    try:
        with _SCRAPE_LOCK:
            page = Fetcher.get(url)
    except Exception:
        return {}
    sections, current_dj = {}, None
    for tr in page.css("tr"):
        b_tag = tr.css("td.tdblue b")
        if b_tag:
            raw = re.sub(r"<[^>]+>", "", b_tag[0].html_content).strip()
            m = re.match(r"(.+?)\s*-\s*(January|February|March|April|May|June|"
                         r"July|August|September|October|November|December)\s+\d{4}", raw)
            if m:
                current_dj = m.group(1).strip()
                sections.setdefault(current_dj, 0)
                continue
        if tr.css("td.tdheadings"):
            continue
        tds = tr.css("td")
        if len(tds) >= 2:
            artist = (tds[0].css("::text").get() or "").strip()
            title = (tds[1].css("::text").get() or "").strip()
            if artist and title and artist not in ("Artist", "&nbsp;"):
                sections[current_dj] = sections.get(current_dj, 0) + 1
    return sections


def _kexp_get(path, timeout=30):
    req = urllib.request.Request(f"https://api.kexp.org/v2{path}",
                                 headers={"User-Agent": "hermes-agent/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def kexp_episode_ids(month, show):
    """Set of /shows/ ids in `month` matching `show` (program_name OR
    host_names, case-insensitive substring). None on API failure; empty set
    = no matching shows. /shows/ serves newest→first regardless of ANY
    filter param (start_time_after/ordering/program all IGNORED — verified
    23 Aug 2026), so paginate backwards until start_time < month-start.
    The old hard cap (offset < 800) was a TIME BOMB: ~9 shows/day means the
    reachable window shrinks daily — April 2026 became unreachable by
    22 Aug ("No trackplays found" on every --dj run). Now boundary-driven
    with a full-archive safety ceiling (20000 ≈ back to 2001)."""
    ids = set()
    offset = 0
    while offset < 20000:
        try:
            d = _kexp_get(f"/shows/?limit=100&offset={offset}")
        except Exception:
            return None
        results = d.get("results") or []
        if not results:
            break
        show_l = show.lower()
        boundary = False
        for sh in results:
            st = (sh.get("start_time") or "")
            if st < f"{month}-01T00:00:00":
                boundary = True
                break
            if not st.startswith(month):
                continue
            hosts = " ".join(sh.get("host_names") or [])
            if show_l in (sh.get("program_name") or "").lower() or show_l in hosts.lower():
                ids.add(sh["id"])
        if boundary:
            break
        offset += 100
    return ids


def kexp_play_walk(month, show_ids=None, max_pages=100):
    """All trackplays in `month` via the /plays/ airdate window query.

    Access-path research (15 Aug 2026, see references/kexp-api.md): /plays/
    IGNORES show=/date= filters (show=, show_uri=, start_time=, airdate=,
    program=...) — every unfiltered call returns the newest ~300 plays (the
    L6 rotation-pool bug). The WORKING query params are `airdate_after=` /
    `airdate_before=` (verified: July 2026 window = 12,458 plays in 13 pages
    of limit=1000), `ordering=` (ascending reaches back to 2000-12-31),
    `artist=`/`song=`/`album=` (true historical search), and offset
    pagination. So: query the month window at limit=1000, filter client-side
    for play_type == "trackplay" and (optionally) numeric show id in
    show_ids. Returns [(artist, title), ...]; None on API failure.
    """
    after = f"{month}-01T00:00:00-07:00"
    y, m = month.split("-")
    before = f"{int(y) + 1}-01-01T00:00:00-07:00" if m == "12" \
        else f"{y}-{int(m) + 1:02d}-01T00:00:00-07:00"
    out, offset, pages = [], 0, 0
    while pages < max_pages:
        try:
            d = _kexp_get(
                f"/plays/?airdate_after={after}&airdate_before={before}"
                f"&limit=1000&offset={offset}")
        except Exception:
            return None
        res = d.get("results") or []
        if not res:
            break
        # Fail-loud guard: the window filter must be honored — if a full
        # page contains NO in-month airdates, the API is ignoring the
        # filters (regression) and we'd otherwise collect ~100k wrong plays.
        if not any((p.get("airdate") or "").startswith(month) for p in res):
            return None
        for p in res:
            if not (p.get("airdate") or "").startswith(month):
                continue
            if p.get("play_type") != "trackplay":
                continue
            if show_ids is not None and p.get("show") not in show_ids:
                continue
            artist = (p.get("artist") or "").strip()
            title = (p.get("song") or "").strip()
            if not artist or not title:
                continue
            out.append((artist, title))
        if len(res) < 1000:
            break
        offset += 1000
        pages += 1
    return out


def kexp_expected(month, show=None):
    """KEXP trackplay count for a month (optionally one show/program).

    True per-month/per-show counts via the /plays/ airdate window
    (kexp_play_walk). Replaces the old rotation-pool approximation
    (~233-240 for every month/show — /plays/ ignored the show filter, so
    each call returned the newest ~300 plays regardless). ~13 pages/month
    ≈ 30-60s.
    """
    try:
        show_ids = None
        if show:
            show_ids = kexp_episode_ids(month, show)
            if show_ids is None:
                return {}
        plays = kexp_play_walk(month, show_ids=show_ids)
        if plays is None:
            return {}
        seen, count = set(), 0
        for artist, title in plays:
            key = f"{artist.lower()}|{title.lower()}"
            if key not in seen:
                seen.add(key)
                count += 1
        return {"The Midday Show" if not show else show: count}
    except Exception:
        return {}


_kexp_opts_cache = {"ts": 0, "programs": [], "hosts": []}


def kexp_options():
    """Return (programs, hosts) — all KEXP shows and DJs, cached 6h."""
    now = time.time()
    if _kexp_opts_cache["programs"] and now - _kexp_opts_cache["ts"] < 21600:
        return _kexp_opts_cache["programs"], _kexp_opts_cache["hosts"]

    def fetch_all(path, key):
        out, offset = [], 0
        while offset < 3000:
            req = urllib.request.Request(
                f"https://api.kexp.org/v2/{path}?limit=100&offset={offset}",
                headers={"User-Agent": "hermes-agent/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    d = json.loads(r.read())
            except Exception:
                break
            results = d.get("results") or []
            if not results:
                break
            for item in results:
                v = (item.get(key) or "").strip()
                if v:
                    out.append(v)
            offset += 100
        return sorted(set(out))

    programs = fetch_all("programs", "name")
    hosts = fetch_all("hosts", "name")
    _kexp_opts_cache.update(ts=now, programs=programs, hosts=hosts)
    return programs, hosts


def known_months(station="dandelion"):
    """Months the STATION SOURCE has data for — regardless of MA playlists.

    This is what makes pre-creation expected counts possible (23 Aug 2026):
    a month with no MA playlists never appears in playlist_snapshot, so the
    dashboard had nothing to attach an expected count to until AFTER the
    first build. Sources, cheapest-first:
      - dandelion: months already scraped into the expected-count cache
        (the scrape itself is triggered by the dashboard for months it
        knows about; keys are 'dandelion:YYYY-MM[...]').
      - kexp: always the current + previous month (24/7 station — data
        exists for every month; no scrape needed to know that).
    Returns a set of 'YYYY-MM'. Never touches the network.
    """
    months = set()
    if station == "kexp":
        today = time.gmtime()
        y, m = today.tm_year, today.tm_mon
        for delta in (0, 1):  # current month, previous month
            tot = y * 12 + (m - 1) - delta
            months.add(f"{tot // 12:04d}-{tot % 12 + 1:02d}")
        return months
    pref = f"{station}:"
    for key in _EXPECTED_KEYS_PROVIDER():
        if key.startswith(pref):
            parts = key.split(":")
            if len(parts) >= 2 and re.match(r"^\d{4}-\d{2}$", parts[1]):
                months.add(parts[1])
    return months


# Set by dandelion-dash.py at import time: a zero-arg callable returning the
# current expected-cache keys. The lib can't import the dash module back
# (hyphen filename, run-as-script) — dependency injection instead. Standalone
# consumers (station scripts importing this lib directly) get an empty
# snapshot, which is correct: they never populate that cache.
_EXPECTED_KEYS_PROVIDER = list


def station_djs(station="dandelion"):
    """All DJ/show names known for a station (from MA playlists), cleaned."""
    snapshot, _ = playlist_snapshot(station)
    djs = set()
    for shows in snapshot.values():
        for dj in shows.keys():
            clean = re.sub(r"\s*\(\d+\)\s*$", "", dj).strip()
            djs.add(clean)
    return sorted(djs)
