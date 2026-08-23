#!/usr/bin/env python3
"""Dandelion/KEXP radio dashboard server — status API + manual trigger.

Endpoints:
  GET  /                       dashboard HTML (station tabs)
  GET  /api/status?station=X   JSON: months -> DJ shows (tracks, liked, expected)
  POST /api/trigger            {station, month, dj?, mode} -> launch station script
  GET  /api/runs               recent trigger runs
  GET  /health                 ok

SECURITY NOTE (M6, decision 15 Aug 2026): POST /api/trigger is intentionally
UNAUTHENTICATED — anyone who can reach djs.robertsons.cloud can launch playlist
runs against Music Assistant. Reviewed and deliberately left open at the user's
request (home dashboard behind their own domain). The 2-run concurrency cap is
the only abuse mitigation. If this ever faces the public internet, add a token.
"""
import json
import os
import re
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dandelion_dash_lib as lib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ma_playlist_lib as mplib

PORT = int(os.environ.get("DANDELION_DASH_PORT", "9210"))
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dandelion-dash.html")
STATUS_TTL = 90
SCRAPE_TTL = 3600
_status_lock = threading.Lock()
_status_cache = {"ts": 0, "data": {}}
# L4 (15 Aug 2026): per-key TTL. Each month keeps its OWN timestamp — the old
# single global "ts" meant ANY stale month wiped the entire cache (all
# stations, all months) and forced a re-scrape storm (KEXP ~6min/month × N)
# after every idle hour. Format: {key: {"ts": float, "value": {dj: count}}}.
_expected_cache = {"data": {}}
_expected_locks = {}
_cache_lock = threading.Lock()   # guards _expected_cache["data"] during disk saves (L1)

# Pre-creation expected counts (23 Aug 2026): lib.known_months("dandelion")
# discovers months from THIS cache. The lib cannot import this module back
# (hyphen filename, run-as-script) — hand it a live key snapshot instead.
lib._EXPECTED_KEYS_PROVIDER = lambda: list(_expected_cache["data"].keys())

STATION_SCRIPT = {
    "dandelion": "dandelion-to-ma.py",
    "kexp": "kexp-to-ma.py",
}


def _load_expected_disk():
    """Load persisted expected counts (survives restarts — KEXP scrape is slow).

    Handles both the legacy format ({key: {dj: count}} from before the L4
    per-key-TTL change) and the current one ({key: {"ts", "value"}}). Legacy
    entries are stamped with now so the upgrade doesn't trigger an immediate
    full re-scrape.
    """
    try:
        with open(lib.CACHE_FILE) as f:
            d = json.load(f)
        conv = {}
        for k, v in (d.get("data", {}) or {}).items():
            if isinstance(v, dict) and "ts" in v and "value" in v:
                conv[k] = v
            else:
                conv[k] = {"ts": time.time(), "value": v}
        # Per-segment keying (row s): drop legacy month-only kexp keys
        # ("kexp:YYYY-MM") — never looked up again, would linger forever.
        # (Pre-creation totals, 23 Aug 2026, live under "<station>-precreate:
        # <month>" — a different prefix, so this filter never touches them.)
        conv = {k: v for k, v in conv.items()
                if not (k.startswith("kexp:") and k.count(":") == 1)}
        _expected_cache["data"] = conv
    except Exception:
        pass


def _save_expected_disk():
    # L1 (15 Aug 2026): serialize the payload under _cache_lock so a
    # concurrent scrape's store can't interleave mid-iteration (was a
    # swallowed RuntimeError -> occasional missed persistence).
    # PR4 (24 Aug 2026): atomic tmp+replace write — concurrent readers
    # (restart-time _load_expected_disk, external tooling) never see a
    # truncated file; two scrape threads can no longer interleave writes.
    try:
        with _cache_lock:
            data = {"data": _expected_cache["data"]}
        mplib.atomic_write_json(lib.CACHE_FILE, data)
    except Exception:
        pass


def build_status(station):
    now = time.time()
    with _status_lock:
        cached = _status_cache["data"].get(station)
        if cached and now - _status_cache["ts"] < STATUS_TTL:
            return cached
        snapshot, err = lib.playlist_snapshot(station)
        if err:
            return {"error": err}
        data = {
            "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
            "favourites_total": len(lib.get_favourites()[0]),
            "months": snapshot,
            "expected": {},
            "unavailable": unavailable_summary(station),
            "runs": lib_run_state(),
            "station": station,
        }
        _status_cache["data"][station] = data
        _status_cache["ts"] = now
        return data


def unavailable_summary(station):
    """{month: {dj: {count, tracks: [{artist,title,reason,attempts,last_seen}]}}}.

    Feature 1 (23 Aug 2026): from the shared unavailable-tracks store — the
    radio-tracklist songs that never matched ANY MA provider at build time.
    Previously this category was console-only and vanished when the run
    exited; now it survives in /root/.hermes/data/unavailable-tracks.json and
    fill-mode runs reconcile it. Grouped per month+DJ so each card can show
    its own red 'unavailable N' chip. Read straight from disk every status
    build: the file is small (tens of entries) and writes are rare.
    """
    try:
        store = mplib.load_unavailable()
    except Exception:
        return {}
    out = {}
    for e in store.values():
        if e.get("station") != station:
            continue
        month, dj = e.get("month"), e.get("dj")
        if not month or not dj:
            continue
        slot = out.setdefault(month, {}).setdefault(dj, {"count": 0, "tracks": []})
        slot["count"] += 1
        slot["tracks"].append({
            "artist": e.get("artist", ""), "title": e.get("title", ""),
            "reason": e.get("reason", "no_match"),
            "attempts": e.get("attempts", 1),
            "last_seen": e.get("last_seen", ""),
        })
    for month in out:
        for dj in out[month]:
            out[month][dj]["tracks"].sort(key=lambda t: t["artist"].lower())
    return out


def lib_run_state():
    runs = []
    if os.path.isdir(lib.RUNS_DIR):
        for f in sorted(os.listdir(lib.RUNS_DIR), reverse=True)[:10]:
            if f.endswith(".json"):
                try:
                    with open(os.path.join(lib.RUNS_DIR, f)) as fh:
                        r = json.load(fh)
                    # reconcile: a 'running' record whose unit is gone means the
                    # run was killed (restart/cgroup) — mark it rather than
                    # leaving a zombie 'running' forever
                    if r.get("status") == "running" and r.get("id"):
                        # unit names are sanitized at launch (M4) — mirror it
                        unit = "dandelion-run-" + re.sub(r"[^A-Za-z0-9_.-]", "_", r["id"])
                        try:
                            chk = subprocess.run(["systemctl", "is-active", unit],
                                                 capture_output=True, text=True, timeout=8)
                            if chk.stdout.strip() not in ("active", "activating", "deactivating"):
                                r["status"] = "died"
                                r["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
                                # PR4: atomic — /api/runs readers never see a torn record
                                mplib.atomic_write_json(os.path.join(lib.RUNS_DIR, f), r)
                        except Exception:
                            pass
                    runs.append(r)
                except Exception:
                    pass
    return runs


def get_expected(station, month, show=None):
    """Return cached expected counts; re-scrape missing/stale months in background.

    Per-key TTL (L4, 15 Aug 2026): each month is tracked by its own timestamp
    instead of one global ts — previously ANY stale month wiped the whole
    cache and every month re-scraped (KEXP ≈6min/month × N months). A stale
    month keeps serving its previous value while the re-scrape runs in the
    background, so the site never shows "expected: —" after an idle hour.

    Per-segment keying (row s, 15 Aug 2026): for KEXP the cache key is
    'station:month:show' and the scrape filters episodes by that show/DJ name
    (lib.kexp_expected(month, show=...)) — each playlist card's expected count
    is scraped for ITS OWN segment ("Cheryl Waters", "Astral Plane", ...)
    instead of one hardcoded "The Midday Show" label that never matched the
    cards (every KEXP card showed "expected ?"). Dandelion ignores show — one
    month-wide {dj: count} dict serves all cards.

    Returns {} immediately for a month that was never cached — the scrape
    runs in a daemon thread and the frontend picks it up on its next poll.
    Never blocks the status request.
    """
    now = time.time()
    key = f"{station}:{month}" if not show else f"{station}:{month}:{show}"
    entry = _expected_cache["data"].get(key)
    if entry and now - entry["ts"] < SCRAPE_TTL:
        return entry["value"]
    # Guard against duplicate concurrent scrapes of the same key.
    lock = _expected_locks.setdefault(key, threading.Lock())
    if lock.locked():
        return entry["value"] if entry else {}
    lock.acquire()

    def _scrape():
        try:
            if station == "kexp":
                res = lib.kexp_expected(month, show=show)
            else:
                res = lib.scrape_expected(month)
            # M1 (15 Aug 2026): only cache NON-EMPTY results. The lib
            # scrapers return {} both for "month has no sections" AND for
            # internal API failures; caching {} stamped fresh would blank the
            # card's expected count for a full hour after one transient
            # error. Empty results are left uncached -> the next poll (30s)
            # retries. (A legitimately-empty month has no playlists, hence no
            # cards, so never serving it is harmless.) The per-key lock still
            # prevents concurrent scrapes of the same key.
            if res:
                with _cache_lock:
                    _expected_cache["data"][key] = {"ts": time.time(), "value": res}
                _save_expected_disk()
        except Exception:
            pass
        finally:
            try:
                lock.release()
            except RuntimeError:
                pass

    threading.Thread(target=_scrape, daemon=True).start()
    return entry["value"] if entry else {}


def _fuzzy_dj_match(segment, expected_map):
    """Best site-header key for a partial-DJ card segment, or None.

    The frontend looks up expected[segment] exactly; playlist-name segments
    are sometimes SHORTER than the site header the count was scraped under
    ("Mark Whitby" card vs "Mark Whitby on FSK" header — created from a
    partial --dj filter). Match when the card segment is a normalized PREFIX
    of a header; exact normalized equality wins outright; two headers sharing
    the prefix = ambiguous -> None (leave "?"). The reverse direction (card
    LONGER than a header, e.g. "Leo Gilbert Again" vs "Leo Gilbert") is
    deliberately NOT matched — such cards are usually a different show that
    merely shares a name prefix. Returns a KEY of expected_map.
    """
    seg_n = re.sub(r"[^a-z0-9]+", "", (segment or "").lower())
    if not seg_n:
        return None
    best, tied = None, False
    for h in expected_map:
        h_n = re.sub(r"[^a-z0-9]+", "", (h or "").lower())
        if not h_n:
            continue
        if h_n == seg_n:
            return h
        if h_n.startswith(seg_n):
            if best is None:
                best = h
            else:
                tied = True  # multiple headers share this prefix — ambiguous
    return None if tied else best


def active_run_count():
    """Count live systemd-run units (concurrency cap). -1 if undeterminable."""
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=active",
             "--no-legend", "--plain", "dandelion-run-*"],
            capture_output=True, text=True, timeout=10).stdout
        return len([l for l in out.splitlines() if l.strip()])
    except Exception:
        return -1


def launch_run(station, month, dj=None, dry_run=False, fill=False, resume=True):
    os.makedirs(lib.RUNS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"{station}-{month}-{dj or 'all'}-{ts}".replace(" ", "_").replace("/", "-")
    log = os.path.join(lib.RUNS_DIR, f"{run_id}.log")
    rec = {"id": run_id, "station": station, "month": month, "dj": dj,
           "dry_run": dry_run, "fill": fill,
           "started": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "running",
           "log": log, "pid": None}
    # PR4: atomic — the /api/runs poller can list this dir the instant we return
    mplib.atomic_write_json(os.path.join(lib.RUNS_DIR, f"{run_id}.json"), rec)

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          STATION_SCRIPT.get(station, "dandelion-to-ma.py"))
    inner = ["/root/.hermes/scripts/scrapling_venv/bin/python3", script,
             "--month", month, "--delay", "0.1"]
    if fill:
        inner += ["--fill"]
    elif resume:
        inner += ["--resume"]
    if dj:
        inner += ["--dj", dj]
    if dry_run:
        inner += ["--dry-run"]

    # Run in its OWN transient systemd unit (--collect auto-removes on exit).
    # A bare Popen child lives in this service's cgroup and is SIGKILLed on
    # every dashboard restart (killed the Jan 2026 build mid-search, 13 Aug).
    # M4: systemd unit names reject ':' '@' and other chars — DJ/show names
    # can contain them, so sanitize the UNIT name (the run_id in the JSON
    # record stays human-readable; only the unit gets scrubbed).
    unit_safe = re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)
    unit = f"dandelion-run-{unit_safe}"
    sdr = ["systemd-run", "--collect", "--wait", "--unit", unit,
           "--property=StandardOutput=append:" + log,
           "--property=StandardError=append:" + log] + inner
    proc = subprocess.Popen(sdr, start_new_session=True, close_fds=True)
    rec["pid"] = proc.pid

    def _watch(p, rec_path):
        rc = p.wait()
        st = "done" if rc == 0 else f"exit-{rc}"
        try:
            with open(rec_path) as f:
                r = json.load(f)
            r["status"] = st
            r["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # PR4: atomic — concurrent /api/runs reads never see the torn state
            mplib.atomic_write_json(rec_path, r)
        except Exception:
            pass
    threading.Thread(target=_watch, args=(proc, os.path.join(lib.RUNS_DIR, f"{run_id}.json")),
                     daemon=True).start()
    return rec


def fill_expected(data, station, known_months=None):
    """Attach per-card expected counts to a status payload (row s, 15 Aug 2026).

    Expected counts are keyed by the SAME segment the cards use, so the
    frontend's exact `exp[dj]` lookup resolves:
      - KEXP: per-show segment ("Cheryl Waters", "Astral Plane", ...) — each
        gets its own scrape filtered to that show/DJ name via
        get_expected(station, month, show=segment). Previously every month
        was labeled "The Midday Show" regardless of the playlist's actual
        name segment → every card showed "expected ?".
      - Dandelion: one month-wide {dj: count} dict, plus a fuzzy fallback
        (_fuzzy_dj_match) for cards whose name segment is a partial of the
        site header ("Mark Whitby" card -> "Mark Whitby on FSK" count).

    Pre-creation expected counts (23 Aug 2026): `data["months"]` only ever
    contains months that ALREADY have MA playlists — fill_expected iterates
    it, so a new month's cards could never show "expected N" before the
    first build. `known_months` (from lib.known_months) lists months the
    STATION SOURCE has data for; for any of those missing from the payload,
    a "__precreate__" entry is attached so the frontend can render a
    build-me placeholder with its expected total.

    L3 (15 Aug 2026): returns a SHALLOW COPY of the payload with a fresh
    "expected" dict — the object build_status caches is never mutated, so
    concurrent status requests can't race on it.
    """
    out = dict(data)
    out["expected"] = {}
    for month, shows in data.get("months", {}).items():
        if station == "kexp":
            exp = {}
            for segment in shows:
                v = get_expected(station, month, show=segment)
                exp[segment] = (v or {}).get(segment) if isinstance(v, dict) else v
        else:
            exp = dict(get_expected(station, month) or {})
            for segment in shows:
                if segment not in exp:
                    m = _fuzzy_dj_match(segment, exp)
                    if m:
                        exp[segment] = exp[m]
        out["expected"][month] = exp

    # Pre-creation months: station source has data, no playlists exist yet.
    # Totals are cached under their own key namespace — "<station>-precreate:
    # <month>" — NOT the bare "station:month" keys, which for kexp collide
    # with legacy {dj: count} entries that _load_expected_disk must keep
    # pruning. get_expected handles persistence + TTL + locking; only the
    # value shape differs ({__total__: n} instead of {dj: count}).
    if known_months:
        have = data.get("months") or {}
        for month in sorted(known_months):
            if month in have:
                continue

            def _precreate_value():
                if station == "kexp":
                    v = get_expected("kexp", month,
                                     show="Cheryl Waters")  # default show until built
                    total = (v or {}).get("Cheryl Waters")
                else:
                    total = sum((get_expected(station, month) or {}).values()) or None
                return {"__precreate__": True, "total": total or None}

            pc_key = f"{station}-precreate:{month}"
            now2 = time.time()
            entry = _expected_cache["data"].get(pc_key)
            if entry and now2 - entry["ts"] < SCRAPE_TTL:
                out["expected"][month] = entry["value"]
                continue
            lock = _expected_locks.setdefault(pc_key, threading.Lock())
            if lock.locked():
                out["expected"][month] = {"__precreate__": True, "total": None}
                continue
            with lock:
                val = _precreate_value()
                out["expected"][month] = val
                # M1 semantics: cache only REAL totals. A None total means the
                # underlying scrape hasn't landed yet (get_expected scrapes in
                # a background thread and returns {}) — caching that pinned
                # "counting…" for a full hour. Leave uncached; the next
                # status poll recomputes and finds it once the scrape lands.
                if val.get("total") is not None:
                    with _cache_lock:
                        _expected_cache["data"][pc_key] = {"ts": time.time(),
                                                           "value": val}
                    _save_expected_disk()
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        query = {}
        if "?" in self.path:
            for kv in self.path.split("?", 1)[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    query[k] = v
        if path in ("/", "/index.html"):
            try:
                with open(HTML_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"dashboard html missing")
        elif path.startswith("/static/"):
            name = os.path.basename(path)
            fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", name)
            try:
                with open(fpath, "rb") as f:
                    body = f.read()
                ctype = {"ico": "image/x-icon", "png": "image/png",
                         "jpg": "image/jpeg", "jpeg": "image/jpeg",
                         "svg": "image/svg+xml"}.get(name.rsplit(".", 1)[-1], "application/octet-stream")
                self._send(200, body, ctype)
            except OSError:
                self._send(404, b'{"error":"not found"}')
        elif path == "/health":
            self._send(200, b'{"ok":true}')
        elif path.startswith("/api/status"):
            station = query.get("station", "dandelion")
            data = fill_expected(build_status(station), station,
                                 known_months=lib.known_months(station))
            self._send(200, json.dumps(data).encode())
        elif path.startswith("/api/options"):
            station = query.get("station", "dandelion")
            if station == "kexp":
                programs, hosts = lib.kexp_options()
                self._send(200, json.dumps({"shows": programs, "djs": hosts}).encode())
            else:
                self._send(200, json.dumps({"shows": [], "djs": lib.station_djs("dandelion")}).encode())
        elif path.startswith("/api/runs"):
            self._send(200, json.dumps(lib_run_state()).encode())
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path.startswith("/api/trigger"):
            ln = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                req = {}
            month = req.get("month") or ""
            if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
                self._send(400, json.dumps({"error": "month must be YYYY-MM"}).encode())
                return
            station = req.get("station", "dandelion")
            dj = req.get("dj") or None
            dry = bool(req.get("dry_run"))
            fill = bool(req.get("fill"))
            mode = req.get("mode") or ("dry" if dry else "fill" if fill else "auto")
            # concurrency cap: max 2 live runs (matches MA's 2-slot task queue)
            n_active = active_run_count()
            if n_active >= 2:
                self._send(429, json.dumps({
                    "ok": False,
                    "error": f"busy — {n_active} runs already active (max 2). "
                             f"Wait or cancel before triggering another."}).encode())
                return
            existing = lib.existing_for(month, dj, station)
            if existing is None:
                # Fail-loud (23 Aug 2026): MA unreachable — cannot tell
                # "no playlists" from "MA down", so auto/dry mode could
                # misroute a fill into create. Abort instead of guessing.
                self._send(503, json.dumps({
                    "ok": False,
                    "error": "Music Assistant unreachable — cannot determine "
                             "existing playlists. Retry once MA is back."}).encode())
                return
            if mode == "auto":
                fill = bool(existing)
                resume = not fill
            elif mode == "fill":
                fill = True
                resume = False
            elif mode == "live":
                fill = False
                resume = False
            else:  # mode == "dry"
                dry = True
                fill = bool(existing)
                resume = not fill
            rec = launch_run(station, month, dj, dry, fill=fill, resume=resume)
            rec["mode"] = mode
            rec["existing_playlists"] = len(existing)
            self._send(200, json.dumps({"ok": True, "run": rec}).encode())
        else:
            self._send(404, b'{"error":"not found"}')


def main():
    _load_expected_disk()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Dandelion/KEXP dashboard on :{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
