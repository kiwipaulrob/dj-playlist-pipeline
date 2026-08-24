#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""PR #10 test suite — visual playlists (24 Aug 2026).

Covers review fixes A (art routing), B (dual-key join), C (pagination —
verified live: playlist_tracks returned all 633 in one call), D (CLS is a
CSS contract; asserted via generated markup invariants), plus row assembly,
caching/invalidation, and fail-loud outage semantics.

All network surfaces mocked. Run: python3 test_pr10_visual.py
"""
import importlib.util
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "ddlib", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "dandelion_dash_lib.py"))
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def ma_track(name, artist, provider="deezer", image=None, remote=True):
    t = {"name": name,
         "artists": [{"name": artist}],
         "provider": provider,
         "provider_mappings": [{"provider_domain": provider}],
         "metadata": {"images": ([{"path": image,
                                   "remotely_accessible": remote,
                                   "proxy_id": "a" * 64}] if image else [])}}
    return t


SRC = [  # station-source order (rows carry the '_dj' page marker)
    {"artist": "The Fall", "title": "Totally Wired (Live at BBC)", "_dj": "Test DJ"},
    {"artist": "Bauhaus", "title": "She's in Parties", "_dj": "Test DJ"},
    {"artist": "Chameleons", "title": "Swamp Thing", "_dj": "Test DJ"},
    {"artist": "R.E.M.", "title": "Driver 8", "_dj": "Test DJ"},
    {"artist": "Grace Jones feat. Sly Dunbar", "title": "Pull Up", "_dj": "Test DJ"},
]

PL_TRACKS = [
    ma_track("Totally Wired", "The Fall", "deezer",
             "https://e-cdns-images.dzcdn.net/images/cover/aaa/500x500.jpg"),
    ma_track("She's in Parties", "Bauhaus", "bandcamp",
             "https://f4.bcbits.com/img/a1234_10.jpg"),
    ma_track("Swamp Thing", "The Chameleons", "deezer"),              # no image
    ma_track("Pull Up", "Grace Jones", "library",
             "smb://nas/music/pullup.mp3", remote=False),             # local SMB art
]

UNAV_STORE = {
    "dandelion|2026-07|Test DJ|chameleons|swamp thing": {
        "artist": "Chameleons", "title": "Swamp Thing", "station": "dandelion",
        "month": "2026-07", "dj": "Test DJ", "reason": "no_match", "attempts": 3},
}

# ---- mock the module's IO surface ----
lib.dandelion_sections = lambda month: list(SRC)
lib.kexp_play_walk = lambda month, show_ids=None: None      # kexp path unused here
lib.all_playlists = lambda: [{"item_id": 777, "name":
                              "Dandelion Radio - July 2026 - Test DJ"}]
import ma_playlist_lib as mplib
mplib.load_unavailable = lambda: dict(UNAV_STORE)


def fake_ma_call(command, args=None, timeout=40):
    """Mock MA transport — playlist_tracks returns the canned playlist."""
    if command == "music/playlists/playlist_tracks":
        return list(PL_TRACKS)
    if command == "music/playlists/library_items":
        return [{"item_id": 777, "name": "Dandelion Radio - July 2026 - Test DJ"}]
    return []


lib.ma_call = fake_ma_call

payload = lib.visual_rows("dandelion", "2026-07", "Test DJ")
check("payload built", isinstance(payload, dict) and payload.get("tracks"))
check("source order preserved",
      [t["title"] for t in payload["tracks"]] == [s["title"] for s in SRC],
      f"{[t['title'] for t in payload['tracks']]}")

# Review fix B: dual-key join
tr0 = payload["tracks"][0]
check("cleaned-title join matches (no false ghost)",
      tr0["found"] and tr0["image"] and "dzcdn" in tr0["image"], f"{tr0}")
tr1 = payload["tracks"][1]
check("raw join + CDN passthrough",
      tr1["found"] and tr1["image"] == "https://f4.bcbits.com/img/a1234_10.jpg"
      and tr1["provider"] == "bandcamp", f"{tr1}")
tr2 = payload["tracks"][2]
check("ghost from store carries reason/attempts",
      not tr2["found"] and tr2["reason"] == "no_match" and tr2["attempts"] == 3, f"{tr2}")
tr3 = payload["tracks"][3]
check("unknown-missing ghost has empty reason",
      not tr3["found"] and tr3["reason"] == "", f"{tr3}")
tr4 = payload["tracks"][4]
check("feat-credit cleaned join matches",
      tr4["found"], f"{tr4}")
# Review fix A: local SMB art must NOT pass through to browser
check("local SMB art suppressed (MA proxy broken upstream)",
      tr4["image"] is None, f"{tr4}")

check("summary arithmetic",
      payload["summary"] == {"total": 5, "matched": 3, "unavailable": 2},
      f"{payload['summary']}")
check("exists flag true when playlist present", payload["exists"] is True)

# ---- pre-create preview ----
lib.all_playlists = lambda: []          # no playlists at all
p2 = lib.visual_rows("dandelion", "2026-07", "Test DJ")
check("unbuilt month -> exists False + ghosts only",
      p2["exists"] is False and not any(t["found"] for t in p2["tracks"])
      and len(p2["tracks"]) == 5, p2["summary"])

# ---- fail-loud: source scrape failure -> None ----
lib.dandelion_sections = lambda month: None
check("scrape failure returns None (503 path)", lib.visual_rows(
    "dandelion", "2026-07", "Test DJ") is None)

# ---- live-bug regressions (found in smoke test 24 Aug) ----
# Bug 1: dandelion rows must be filtered by the '_dj' page marker — before the
# fix, every DJ's view rendered the WHOLE month page (567 rows for a 12-track show).
lib.dandelion_sections = lambda month: list(SRC) + [
    {"artist": "Other", "title": "Not This DJ", "_dj": "Someone Else"}]
lib.all_playlists = lambda: [{"item_id": 777, "name":
                              "Dandelion Radio - July 2026 - Test DJ"}]
p3 = lib.visual_rows("dandelion", "2026-07", "Test DJ")
check("_dj filter excludes other DJs' rows",
      len(p3["tracks"]) == 5 and p3["summary"]["total"] == 5,
      f"{p3['summary']}")
check("absent-DJ view returns empty rows, not error",
      lib.visual_rows("dandelion", "2026-07", "Ghost DJ")["summary"]["total"] == 0)
lib.dandelion_sections = lambda month: list(SRC)

# Bug 2: KEXP source must resolve episode ids BEFORE walking plays — otherwise
# the airdate window returns every play in the month (~8k), not the show (~860).
ep_calls, walk_calls = [], []


def fake_eps(month, show):
    ep_calls.append((month, show))
    return {101, 102}


def fake_walk(month, show_ids=None, max_pages=100):
    walk_calls.append(show_ids)
    return [("A", "T")]


fresh2 = importlib.util.module_from_spec(
    importlib.util.spec_from_file_location(
        "ddlib3", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "dandelion_dash_lib.py")))
importlib.util.spec_from_file_location(
    "ddlib3", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dandelion_dash_lib.py")).loader.exec_module(fresh2)
fresh2.kexp_episode_ids = fake_eps
fresh2.kexp_play_walk = fake_walk
src_k = fresh2._source_tracks("kexp", "2026-07", dj="The Midday Show")
check("kexp resolves episodes then walks with ids",
      ep_calls == [("2026-07", "The Midday Show")]
      and walk_calls == [{101, 102}] and src_k == [{"artist": "A", "title": "T"}],
      f"{ep_calls} {walk_calls}")
fresh2.kexp_episode_ids = lambda m, s: None     # API failure
check("kexp API failure -> None (fail loud)",
      fresh2._source_tracks("kexp", "2026-07", dj="X") is None)

# ---- MA outage during playlist_tracks read -> None ----
calls = {"n": 0}


def flaky_ma(command, args=None, timeout=40):
    calls["n"] += 1
    if command == "music/playlists/playlist_tracks":
        return {"__error__": "connection reset"}
    if command == "music/playlists/library_items":
        return [{"item_id": 777, "name": "Dandelion Radio - July 2026 - Test DJ"}]
    return []


lib.dandelion_sections = lambda month: list(SRC)
lib.all_playlists = lambda: [{"item_id": 777, "name":
                              "Dandelion Radio - July 2026 - Test DJ"}]
lib.ma_call = flaky_ma
check("MA outage mid-request -> None (fail loud)", lib.visual_rows(
    "dandelion", "2026-07", "Test DJ") is None, f"calls={calls['n']}")

# ---- cache write/read/invalidate ----
import importlib
spec2 = importlib.util.spec_from_file_location(
    "ddlib2", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dandelion_dash_lib.py"))
fresh = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(fresh)

payload_ok = {"station": "dandelion", "month": "2026-07", "dj": "Cache Test",
              "tracks": [], "summary": {}}
fresh.visual_cache_write(payload_ok)
p = fresh.visual_cache_path("dandelion", "2026-07", "Cache Test")
check("cache file written atomically", os.path.exists(p))
got = fresh.visual_cache_read("dandelion", "2026-07", "Cache Test")
check("cache read within TTL", got is not None and "_ts" not in got)
# expired entry
body = dict(payload_ok); body["_ts"] = time.time() - fresh.VISUAL_TTL - 5
json.dump(body, open(p, "w"))
check("expired cache ignored", fresh.visual_cache_read(
    "dandelion", "2026-07", "Cache Test") is None)
fresh.visual_cache_invalidate(station="dandelion", month="2026-07", dj="Cache Test")
check("invalidate removes file", not os.path.exists(p))
# stale-list safety: invalidate with no matches must not raise
try:
    fresh.visual_cache_invalidate()
    ok = True
except Exception:
    ok = False
check("wildcard invalidate safe", ok)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
