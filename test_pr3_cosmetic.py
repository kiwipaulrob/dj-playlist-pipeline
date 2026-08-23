"""PR 3 tests — cosmetic cleanup: search_ma dead reuse, _pick_best fallback,
rebuild-months arg parsing (run on CT106)."""
import importlib.util
import os
import subprocess
import sys
from unittest import mock

WORK = os.path.dirname(os.path.abspath(__file__))


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mplib = load_mod("ma_playlist_lib", os.path.join(WORK, "ma_playlist_lib.py"))

ok = fail = 0


def check(name, cond):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name)
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)


# --- a. search_ma: library hit short-circuits (cross-provider never called) --
calls = []


def ds_lib_hit(token, payload):
    calls.append(payload["args"]["library_only"])
    if payload["args"]["library_only"]:
        return {"tracks": [{"name": "Song", "artists": [{"name": "Artist"}],
                            "uri": "deezer://1",
                            "provider_mappings": [{"provider_domain": "deezer"}]}]}
    return {"tracks": []}


with mock.patch.object(mplib, "_do_search", ds_lib_hit), \
     mock.patch.object(mplib, "time") as mt:
    mt.sleep = lambda s: None
    r = mplib.search_ma("tok", "Artist", "Song")
check("library hit returns tuple", r == ("deezer://1", "deezer", "Song"))
check("cross-provider never queried on library hit", set(calls) == {True})

# b. full miss -> returns None (explicit), retries once on failure
seq = []
calls2 = []


def ds_miss_then_fail(token, payload):
    lib = payload["args"]["library_only"]
    calls2.append(lib)
    if not lib:
        seq.append(1)
        return None if len(seq) <= 1 else None  # always fails post-library
    return {"tracks": []}


with mock.patch.object(mplib, "_do_search", ds_miss_then_fail), \
     mock.patch.object(mplib, "time") as mt:
    mt.sleep = lambda s: None
    r2 = mplib.search_ma("tok", "Artist", "Song")
check("total failure returns None", r2 is None)

# c. single-artist track: exactly one library query (no title-only variant)
def counting_ds(token, payload):
    calls3.append(payload["args"]["search_query"])
    return {"tracks": []}


calls3 = []
with mock.patch.object(mplib, "_do_search", counting_ds):
    mplib.search_ma("tok", "Radiohead", "Karma Police")
check("single artist -> one strategy per phase", len(calls3) == 2)

# d. multi-artist credit: two strategies per phase (primary+title, title-only)
calls4 = []
with mock.patch.object(mplib, "_do_search", counting_ds.__wrapped__ if hasattr(counting_ds, "__wrapped__") else (lambda token, payload: (calls4.append(payload["args"]["search_query"]), {"tracks": []})[1])):
    mplib.search_ma("tok", "A, B & C", "Song X")
check("multi artist -> two strategies per phase", len(calls4) == 4)

# --- e. _pick_best: empty/missing provider_mappings no longer [{}] hack ------
res = {"tracks": [{"name": "Real Title", "artists": [{"name": "Real Artist"}],
                   "uri": "x://1", "provider_mappings": []}]}
out = mplib._pick_best(res, "Real Artist", "Real Title")
check("empty mappings -> prov '' (no crash)", out == ("x://1", "", "Real Title"))
res2 = {"tracks": [{"name": "Real Title", "artists": [{"name": "Real Artist"}],
                    "uri": "x://2", "provider_mappings": None}]}
out2 = mplib._pick_best(res2, "Real Artist", "Real Title")
check("missing mappings -> prov ''", out2 == ("x://2", "", "Real Title"))
res3 = {"tracks": [{"name": "Real Title", "artists": [{"name": "Real Artist"}],
                    "uri": "x://3",
                    "provider_mappings": [{"provider_domain": "bandcamp"}]}]}
out3 = mplib._pick_best(res3, "Real Artist", "Real Title")
check("normal mapping extracts domain", out3 == ("x://3", "bandcamp", "Real Title"))

# --- f. rebuild tool: argparse contract ---------------------------------------
env = dict(os.environ, PATH=os.environ.get("PATH", ""))
p = subprocess.run([sys.executable, os.path.join(WORK, "kexp-rebuild-months.py"), "--help"],
                   capture_output=True, text=True, timeout=60, env=env)
check("--help exits 0", p.returncode == 0)
check("--help documents YYYY-MM months", "YYYY-MM" in p.stdout)

p2 = subprocess.run([sys.executable, os.path.join(WORK, "kexp-rebuild-months.py"), "2026-13"],
                    capture_output=True, text=True, timeout=60, env=env)
check("bad month rejected (exit 2)", p2.returncode == 2)
check("bad month message names the value", "2026-13" in (p2.stderr + p2.stdout))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
