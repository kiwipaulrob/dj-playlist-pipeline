"""PR 1 tests — pre-creation expected counts (run on CT106 against edited copies)."""
import importlib.util
import json
import os
import sys
import tempfile
import time
from unittest import mock

WORK = os.path.dirname(os.path.abspath(__file__))

# Optional argv[1] = variant ("pr1"/"pr2"): load <variant>_lib.py /
# <variant>_dash.py instead of the merged files, so this suite proves the
# PR 1 variants are self-sufficient (not just the merged tree).
VARIANT = next((a for a in sys.argv[1:] if not a.startswith("-")), "")


def _vfile(merged, suffix):
    return os.path.join(WORK, f"{VARIANT}_{suffix}.py") if VARIANT \
        else os.path.join(WORK, merged)


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # register BEFORE exec — mirrors production
    spec.loader.exec_module(mod)     # (single shared lib instance, no re-import)
    return mod


lib = load_mod("dandelion_dash_lib", _vfile("dandelion_dash_lib.py", "lib"))
tmpcache = tempfile.NamedTemporaryFile(suffix=".json", delete=False).name
lib.CACHE_FILE = tmpcache                      # redirect BEFORE dash loads it
dash = load_mod("dandelion_dash", _vfile("dandelion-dash.py", "dash"))
assert dash.lib is lib, "dash must share the test's lib instance"
print(f"# variant={VARIANT or 'merged'} lib={lib.__file__} dash={dash.__file__}")

ok = fail = 0


def check(name, cond):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name)
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)


# --- a. known_months(kexp) = current + previous month -----------------------
now = time.gmtime()
tot = now.tm_year * 12 + now.tm_mon - 1
want = {f"{tot // 12:04d}-{tot % 12 + 1:02d}", f"{(tot - 1) // 12:04d}-{(tot - 1) % 12 + 1:02d}"}
check("known_months kexp = cur+prev", lib.known_months("kexp") == want)

# year-boundary arithmetic (mock January 2027)
with mock.patch("time.gmtime", return_value=time.struct_time((2027, 1, 15, 0, 0, 0, 0, 0, 0))):
    check("known_months kexp jan->dec", lib.known_months("kexp") == {"2027-01", "2026-12"})

# --- b. known_months(dandelion) driven by the dash expected-cache -----------
dash._expected_cache["data"] = {
    "dandelion:2025-12": {"ts": 1, "value": {"DJ A": 10}},
    "dandelion:2026-07": {"ts": 1, "value": {"DJ B": 20}},
    "kexp:2026-07:Cheryl Waters": {"ts": 1, "value": {"Cheryl Waters": 600}},
}
check("known_months dandelion from cache",
      lib.known_months("dandelion") == {"2025-12", "2026-07"})
check("known_months unknown station -> empty", lib.known_months("wfmt") == set())

# --- c. fill_expected attaches __precreate__ for missing months -------------
data = {
    "generated": "t", "favourites_total": 0,
    "months": {"2026-07": {"Cheryl Waters": {"id": "1", "name": "n", "tracks": 235,
                                             "liked": 3, "providers": {}, "month": "2026-07",
                                             "year": "2026", "dj": "Cheryl Waters"}}},
    "expected": {}, "runs": [], "station": "kexp",
}
calls = []


def fake_get_expected(station, month, show=None):
    calls.append((station, month, show))
    return {"Cheryl Waters": 651} if show == "Cheryl Waters" else {}


with mock.patch.object(dash, "get_expected", fake_get_expected):
    out = dash.fill_expected(data, "kexp", known_months=lib.known_months("kexp"))
pre = out["expected"].get(f"{tot // 12:04d}-{tot % 12 + 1:02d}")
check("precreate entry attached", isinstance(pre, dict) and pre.get("__precreate__") is True)
check("precreate total from scrape value", pre.get("total") == 651)
check("precreate queried default show", ("kexp", f"{tot // 12:04d}-{tot % 12 + 1:02d}", "Cheryl Waters") in calls)
check("existing month untouched", "__precreate__" not in out["expected"]["2026-07"])
check("cached payload NOT mutated", data.get("expected") == {} and "__precreate__" not in str(data["months"]))
check("known_months=None keeps legacy behavior",
      "__precreate__" not in json.dumps(dash.fill_expected(data, "kexp")))
with mock.patch.object(dash, "get_expected", fake_get_expected):
    out2 = dash.fill_expected(data, "kexp", known_months={"2026-07"})
check("month WITH playlists gets no precreate", "__precreate__" not in out2["expected"].get("2026-07", {}))

# dandelion branch sums the month-wide dict
dd_data = dict(data)
dd_data["station"] = "dandelion"
dd_data["months"] = {}
with mock.patch.object(dash, "get_expected", lambda s, m, show=None: {"DJ A": 10, "DJ B": 32} if m == "2025-12" else {}):
    out3 = dash.fill_expected(dd_data, "dandelion", known_months={"2025-12"})
check("dandelion precreate total = sum", out3["expected"]["2025-12"] == {"__precreate__": True, "total": 42})

# --- d. (removed) _is_precreate_total discriminator — superseded by the
#        precreate key namespace; legacy pruning needs no shape sniffing ----

# --- e. round-trip: pre-create total computed, cached under its own
#        namespace ("<station>-precreate:<month>"), persisted -----------------
dash._expected_cache["data"] = {}
dash.lib.CACHE_FILE = tmpcache

with mock.patch.object(dash, "get_expected",
                       lambda s, m, show=None: {"Cheryl Waters": 77} if show == "Cheryl Waters" else {}):
    out5 = dash.fill_expected({"months": {}, "station": "kexp"}, "kexp",
                              known_months={"2099-01"})
check("fill_expected computes precreate total",
      out5["expected"]["2099-01"] == {"__precreate__": True, "total": 77})
check("cached under precreate namespace",
      dash._expected_cache["data"].get("kexp-precreate:2099-01", {}).get("value")
      == {"__precreate__": True, "total": 77})
disk = json.load(open(tmpcache))
check("persisted to disk",
      disk["data"].get("kexp-precreate:2099-01", {}).get("value")
      == {"__precreate__": True, "total": 77})

# M1 semantics: a None total (scrape not landed yet) renders "counting…"
# but must NOT be cached — otherwise it pins for a full hour.
with mock.patch.object(dash, "get_expected", lambda s, m, show=None: {}):
    out6 = dash.fill_expected({"months": {}, "station": "dandelion"}, "dandelion",
                              known_months={"2098-12"})
check("None total renders counting state",
      out6["expected"]["2098-12"] == {"__precreate__": True, "total": None})
check("None total NOT cached",
      "dandelion-precreate:2098-12" not in dash._expected_cache["data"])

# reload through _load_expected_disk: legacy month-only kexp keys still pruned,
# per-segment keys and precreate-namespace keys both survive.
dash._expected_cache["data"] = {}
with open(tmpcache, "w") as f:
    json.dump({"data": {
        "kexp:2020-05": {"Old DJ": 100},                                  # legacy -> drop
        "kexp:2020-06:Cheryl Waters": {"ts": 1, "value": {"Cheryl Waters": 5}},  # keep
        "kexp-precreate:2099-01": {"ts": 1, "value": {"__precreate__": True, "total": 77}},  # keep
    }}, f)
dash._load_expected_disk()
ks = dash._expected_cache["data"]
check("reload drops true legacy key", "kexp:2020-05" not in ks)
check("reload keeps per-show key", "kexp:2020-06:Cheryl Waters" in ks)
check("reload keeps precreate key",
      ks.get("kexp-precreate:2099-01", {}).get("value", {}).get("total") == 77)

os.unlink(tmpcache)
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
