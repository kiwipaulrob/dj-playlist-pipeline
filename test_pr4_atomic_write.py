#!/root/.hermes/scripts/scrapling_venv/bin/python3
"""PR4 test suite — atomic JSON writes (24 Aug 2026).

Covers:
  1. atomic_write_json basics: roundtrip, dirs auto-created, tmp cleanup on error
  2. CONCURRENCY: N writer threads × M reader threads hammering the same file;
     every read must parse (zero JSONDecodeError) — the exact failure mode the
     PR fixes (plain open('w') truncates before writing)
  3. dandelion-dash.py integration: _save_expected_disk + run-record writes go
     through the atomic helper; concurrent /api/runs-style reads stay valid
  4. _save_unavailable still produces sorted, loadable store files

Run: python3 test_pr4_atomic_write.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time

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


tmp = tempfile.mkdtemp(prefix="pr4-test-")
try:
    # ---------- 1. basics ----------
    p1 = os.path.join(tmp, "sub", "dir", "x.json")   # dirs don't exist yet
    mplib.atomic_write_json(p1, {"k": [1, 2, 3]}, indent=1)
    check("roundtrip", json.load(open(p1)) == {"k": [1, 2, 3]})

    try:
        mplib.atomic_write_json(p1, {"will": "fail", "unserializable": {1, 2}})
        ok = False
    except TypeError:
        ok = True
    leftovers = [f for f in os.listdir(os.path.dirname(p1)) if f.startswith(".tmp-")]
    check("tmp cleaned up on dump error", ok and not leftovers,
          f"ok={ok} leftovers={leftovers}")

    # ---------- 2. concurrency: writers vs readers ----------
    p2 = os.path.join(tmp, "hot.json")
    mplib.atomic_write_json(p2, {"gen": -1})
    stop = time.time() + 4.0
    errors, reads, max_gen = [], [0], [-1]
    rlock = threading.Lock()

    def writer(wid):
        g = 0
        while time.time() < stop:
            g += 1
            mplib.atomic_write_json(p2, {"gen": f"{wid}-{g}", "pad": "x" * 500})
            time.sleep(0.001)

    def reader():
        while time.time() < stop:
            try:
                with open(p2) as f:
                    d = json.load(f)
                with rlock:
                    reads[0] += 1
                    gen = d.get("gen")
                    if isinstance(gen, str) and "-" in gen:
                        n = int(gen.split("-")[1])
                        if n > max_gen[0]:
                            max_gen[0] = n
            except json.JSONDecodeError as e:
                with rlock:
                    errors.append(str(e))
            except FileNotFoundError:
                pass

    wthreads = [threading.Thread(target=writer, args=(w,)) for w in range(3)]
    rthreads = [threading.Thread(target=reader) for _ in range(6)]
    for t in wthreads + rthreads:
        t.start()
    for t in wthreads + rthreads:
        t.join()

    check("concurrent reads all parsed", not errors,
          f"{len(errors)} decode errors e.g. {errors[:2]}")
    check("readers actually exercised", reads[0] > 200, f"reads={reads[0]}")
    check("file left valid", isinstance(json.load(open(p2)).get("gen"), str))

    # ---------- control: NON-atomic write DOES corrupt under this load ----------
    p3 = os.path.join(tmp, "naive.json")
    with open(p3, "w") as f:
        json.dump({"gen": "seed"}, f)
    stop2 = time.time() + 1.2
    naive_err = []

    def naive_writer():
        g = 0
        while time.time() < stop2:
            g += 1
            with open(p3, "w") as f:          # truncate-then-write
                f.write(json.dumps({"gen": g, "pad": "y" * 2000}))

    def naive_reader():
        while time.time() < stop2:
            try:
                with open(p3) as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                naive_err.append(str(e))
            except FileNotFoundError:
                pass

    nw = threading.Thread(target=naive_writer)
    nr = [threading.Thread(target=naive_reader) for _ in range(4)]
    nw.start()
    for t in nr:
        t.start()
    nw.join()
    for t in nr:
        t.join()
    print(f"INFO control group (naive write): {len(naive_err)} decode errors "
          f"in 1.2s — demonstrates the bug class this PR removes")

    # ---------- 3. dash integration ----------
    spec = importlib.util.spec_from_file_location(
        "ddash", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "dandelion-dash.py"))
    dash = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dash)

    cache_file = os.path.join(tmp, "status-cache.json")
    dash.lib.CACHE_FILE = cache_file                       # redirect BEFORE use
    dash._expected_cache["data"] = {"dandelion:2026-07": {"ts": 1, "value": {"DJ": 9}}}
    dash._save_expected_disk()
    check("cache file written via helper",
          json.load(open(cache_file))["data"]["dandelion:2026-07"]["value"] == {"DJ": 9})

    runs_dir = os.path.join(tmp, "runs")
    dash.lib.RUNS_DIR = runs_dir
    rec = dash.launch_run("dandelion", "2026-07", "Test DJ", dry_run=True)
    rp = os.path.join(runs_dir, rec["id"] + ".json")
    check("run record written atomically", json.load(open(rp))["status"] == "running")

    # simulate watcher completion path (no real process): reuse its write logic
    r = json.load(open(rp))
    r["status"] = "done"
    r["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    mplib.atomic_write_json(rp, r)
    state = dash.lib_run_state()
    mine = [x for x in state if x.get("id") == rec["id"]]
    check("lib_run_state reconciles atomic records", mine and mine[0]["status"] == "done",
          f"got {mine}")

    # ---------- 4. unavailable store round-trip ----------
    store_file = os.path.join(tmp, "unavailable.json")
    mplib.UNAVAILABLE_FILE = store_file
    mplib.record_unavailable_many("kexp", "2026-07", [
        ("The Midday Show", "Zed Artist", "Z Track", "no_match"),
        ("The Midday Show", "Alpha Artist", "A Track", "timeout"),
    ])
    raw = json.load(open(store_file))
    keys = list(raw.keys())
    check("store keys sorted on disk", keys == sorted(keys), f"{keys[:3]}…")
    check("store reloads via API", len(mplib.load_unavailable()) == 2)

finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
