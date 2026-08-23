"""PR 2 tests — fail-loud existing_for() + trigger abort (run on CT106)."""
import importlib.util
import json
import os
import sys
from unittest import mock

WORK = os.path.dirname(os.path.abspath(__file__))

# Optional argv[1] = variant ("pr1"/"pr2"): load <variant>_lib.py /
# <variant>_dash.py instead of the merged files, so this suite proves the
# PR 2 variants are self-sufficient (not just the merged tree).
VARIANT = next((a for a in sys.argv[1:] if not a.startswith("-")), "")


def _vfile(merged, suffix):
    return os.path.join(WORK, f"{VARIANT}_{suffix}.py") if VARIANT \
        else os.path.join(WORK, merged)


def load_mod(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lib = load_mod("dandelion_dash_lib", _vfile("dandelion_dash_lib.py", "lib"))
dash = load_mod("dandelion_dash", _vfile("dandelion-dash.py", "dash"))
print(f"# variant={VARIANT or 'merged'} lib={lib.__file__} dash={dash.__file__}")


def _no_real_launch(*a, **k):
    raise RuntimeError(
        "UNMOCKED launch_run called — refusing to spawn a REAL systemd-run "
        "against production. Trigger tests must mock dash.launch_run.")


# Tripwire (23 Aug 2026): running this suite against the pr1 variant let the
# first trigger test fall past the 503 branch into a REAL launch_run — two
# stray kexp-to-ma systemd units hit production before being killed. Any
# launch_run invocation must go through an explicit mock.patch.object below;
# this default makes a slip fail loudly instead of mutating infrastructure.
dash.launch_run = _no_real_launch

ok = fail = 0


def check(name, cond):
    global ok, fail
    print(("PASS " if cond else "FAIL ") + name)
    ok, fail = ok + (1 if cond else 0), fail + (0 if cond else 1)


def fake_page(items):
    """Patch all_playlists to return a canned library (or [] for outage)."""
    return mock.patch.object(lib, "all_playlists", lambda station=None: list(items))


PLS = [
    {"item_id": "1", "name": "KEXP - February 2026 - Cheryl Waters"},
    {"item_id": "2", "name": "KEXP - February 2026 - Cheryl Waters (2)"},
    {"item_id": "3", "name": "Dandelion Radio - June 2026 - Leo Gilbert on FSK"},
    {"item_id": "4", "name": "Some Other Playlist"},
]

# a. happy paths — exact lists per month/station/DJ
with fake_page(PLS):
    check("kexp feb finds both copies",
          lib.existing_for("2026-02", None, "kexp") ==
          ["KEXP - February 2026 - Cheryl Waters", "KEXP - February 2026 - Cheryl Waters (2)"])
with fake_page(PLS):
    check("dj filter narrows",
          lib.existing_for("2026-06", "leo gilbert", "dandelion") ==
          ["Dandelion Radio - June 2026 - Leo Gilbert on FSK"])
with fake_page(PLS):
    check("no match -> empty LIST",
          lib.existing_for("2025-01", None, "dandelion") == [])

# b. outage: empty library + MA answering non-list -> None
def dead_probe(command, args=None, timeout=30):
    return {"__error__": "connection refused"}


with fake_page([]), mock.patch.object(lib, "ma_call", dead_probe):
    check("MA down -> None", lib.existing_for("2026-02", None, "kexp") is None)

# c. genuinely empty library: probe answers with a real list -> []
with fake_page([]), mock.patch.object(lib, "ma_call",
                                      lambda command, args=None, timeout=30: []):
    check("empty library -> empty list (not None)",
          lib.existing_for("2026-02", None, "kexp") == [])

# d. bad month still returns [] (input validation, unchanged)
check("bad month -> []", lib.existing_for("junk", None, "kexp") == [])

# e. trigger endpoint: 503 on None, normal flow otherwise
class FakeHandler(dash.Handler):
    def __init__(self):
        self._resp = None

    def _send(self, code, body, ctype="application/json"):
        self._resp = (code, json.loads(body))


h = FakeHandler()
h.path = "/api/trigger"
req = json.dumps({"station": "kexp", "month": "2026-02", "mode": "auto"}).encode()


def make_read(n):
    return lambda ln: req


with mock.patch.object(lib, "existing_for", return_value=None) as mex, \
     mock.patch.object(dash, "active_run_count", lambda: 0), \
     mock.patch.object(FakeHandler, "do_POST", dash.Handler.do_POST):
    h.rfile = type("R", (), {"read": staticmethod(make_read(len(req)))})()
    h.headers = {"Content-Length": str(len(req))}
    h.do_POST()
code, body = h._resp
check("trigger 503 when existing_for None", code == 503)
check("503 mentions MA unreachable", "unreachable" in body.get("error", ""))
mex.assert_called_once()

captured = {}


def fake_launch(station, month, dj=None, dry_run=False, fill=False, resume=True):
    captured.update(fill=fill, resume=resume)
    return {"id": "x"}


with mock.patch.object(lib, "existing_for", lambda *a, **k: ["KEXP - February 2026 - Cheryl Waters"]), \
     mock.patch.object(dash, "active_run_count", lambda: 0), \
     mock.patch.object(dash, "launch_run", fake_launch):
    h2 = FakeHandler()
    h2.path = "/api/trigger"
    h2.rfile = type("R", (), {"read": staticmethod(make_read(len(req)))})()
    h2.headers = {"Content-Length": str(len(req))}
    h2.do_POST()
code2, body2 = h2._resp
check("healthy MA -> 200 ok", code2 == 200 and body2["ok"] is True)
check("auto mode routed fill=True on existing",
      captured["fill"] is True and captured["resume"] is False)
check("response carries existing_playlists count", body2["run"]["existing_playlists"] == 1)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
