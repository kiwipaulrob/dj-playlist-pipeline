#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""PR-D test suite — post-settle count verification in add_to_existing.

All MA calls monkeypatched (no network). Verifies:
  - default (no playlist_name): behavior byte-identical to before (no extra calls)
  - with playlist_name: start snapshot -> batches -> task wait -> delta check
  - silent-noop detection: submitted 3, only +2 landed -> ⚠️ path, still True
  - unverifiable paths (task timeout / API error) never raise

Run: python3 test_pr8_count_verify.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ma_playlist_lib as mplib

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


class FakeAPI:
    """Records calls; simulates a playlist whose count grows by `landed`."""

    def __init__(self, landed=None, tasks_busy=0):
        self.calls = []
        self.count = 10            # pre-existing tracks
        self.landed = landed       # None = all land; int = only N land
        self.tasks_busy = tasks_busy
        self.submitted = 0

    def __call__(self, token, payload, timeout=60):
        cmd = payload["command"]
        self.calls.append(cmd)
        if cmd == "music/playlists/playlist_tracks":
            return [{"position": i} for i in range(self.count)]
        if cmd == "music/playlists/add_playlist_tracks":
            self.submitted += len(payload["args"]["uris"])
            n = self.landed if self.landed is not None else self.submitted
            self.count = 10 + min(n, self.submitted)
            return {"ok": True}
        if cmd == "tasks/list":
            if self.tasks_busy > 0:
                self.tasks_busy -= 1
                return [{"name": "Add items to playlist X", "status": "running"}]
            return []
        return {}


URIS = [f"uri{i}" for i in range(3)]

# 1. legacy signature — NO extra API traffic beyond the add batches
fake = FakeAPI()
orig_api = mplib._api
mplib._api = fake
ok = mplib.add_to_existing("tok", 42, URIS)
mplib._api = orig_api
check("legacy call returns True", ok is True)
check("no extra calls without playlist_name",
      fake.calls.count("tasks/list") == 0 and
      fake.calls.count("music/playlists/playlist_tracks") == 0,
      f"{fake.calls}")

# 2. full happy path with verification
fake = FakeAPI()
mplib._api = fake
ok = mplib.add_to_existing("tok", 42, URIS, playlist_name="My Playlist")
mplib._api = orig_api
check("verified call returns True", ok is True)
check("snapshot taken before adds",
      fake.calls[0] == "music/playlists/playlist_tracks", f"{fake.calls}")
check("tasks polled until settle", "tasks/list" in fake.calls)
check("count re-read after settle",
      fake.calls[-1] == "music/playlists/playlist_tracks", f"{fake.calls}")

# 3. silent no-op (MA accepted task but fewer tracks landed) -> warning, no raise
fake = FakeAPI(landed=2)
mplib._api = fake
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ok = mplib.add_to_existing("tok", 42, URIS, playlist_name="My Playlist")
mplib._api = orig_api
out = buf.getvalue()
check("silent-noop returns True (never a new failure path)", ok is True)
check("silent-noop flagged in output", "⚠️" in out and "+2" in out, out.strip())

# 4. task-wait timeout path — warns, returns True
fake = FakeAPI(tasks_busy=99)   # never settles within its internal budget
mplib._api = fake
ok = mplib.add_to_existing("tok", 42, ["u1"], playlist_name="My Playlist")
mplib._api = orig_api
check("task-timeout returns True", ok is True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
