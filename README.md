# DJ Playlist Pipeline

Automated radio-station playlist generation for **Music Assistant** (MA): scrape a
station's published tracklists (or read its broadcast API), match every track across
all MA music providers, build one curated playlist per show per month, and serve a
live web dashboard of the results at `djs.robertsons.cloud`.

Two stations are wired up:

| Station | Source | Granularity | Example playlist |
|---|---|---|---|
| **Dandelion Radio** (UK, John Peel-inspired) | monthly tracklist pages @ dandelionradio.com (Scrapling/HTML) | one playlist per DJ show | `Dandelion Radio - July 2026 - Leo Gilbert on FSK` |
| **KEXP** (Seattle public radio) | public JSON API @ api.kexp.org/v2 | one aggregate playlist per show per month | `KEXP - April 2026 - Cheryl Waters` |

Everything runs on a small homelab LXC (CT100) under a Scrapling venv; the dashboard
is a dependency-free Python stdlib HTTP server behind nginx + Cloudflare Tunnel.

---

## Architecture

```
                 ┌──────────────────────────┐        ┌─────────────────────┐
 Dandelion site ─▶│ dandelion-to-ma.py       │        │                     │
 (HTML tables)   │  scrape_month()          ├────────▶ ma_playlist_lib.py  │
                 └──────────────────────────┘ search │  (shared MA plumbing)│
                 ┌──────────────────────────┐   create│                     │
 KEXP API      ──▶│ kexp-to-ma.py            ├────────▶▶ Music Assistant     │
 (/shows/, /plays/) │ kexp_month_tracks()  │  fill    │ :8095 JSON-RPC      │
                 └──────────────────────────┘        └──────────┬──────────┘
                                                                │
                 ┌──────────────────────────┐                    │ reads
 djs.robertsons ◀─┤ dandelion-dash.py (:9210)◀────────────────────┘
  (browser UI)    │  dandelion_dash_lib.py   │  + launches runs via
                 └──────────────────────────┘    systemd-run (isolated units)
```

**Data flow per month:** fetch source tracklist → normalize (`artist`, `title`) →
dedupe (KEXP only — repeated plays across episodes collapse) → for each track:
`music/search` library-first, then cross-provider → rank candidates by provider
priority + title/artist agreement → collect URIs → create playlist (or fill gaps in
an existing one) → wait for MA's background add-tasks → verify track count →
report expected-vs-found.

## Repository layout

| File | Purpose |
|---|---|
| `ma_playlist_lib.py` | **Shared MA plumbing** (single source of truth): token handling, JSON-RPC `_api`, provider-priority search (`search_ma`/`_pick_best`), paginated playlist listing, create/add/remove/wait/verify helpers, fail-loud contract + mid-run outage circuit breaker, **unavailable-tracks store** (`record_unavailable*`/`clear_unavailable*`) |
| `dandelion-to-ma.py` | Dandelion station script: HTML scraping (Scrapling CSS selectors), **absent-DJ detection** (`absent_djs()` — headers that parsed but yielded 0 track rows), `--month/--dj/--fill/--resume/--dry-run` |
| `kexp-to-ma.py` | KEXP station script: airdate-window play walk + client-side episode filtering, same flags |
| `dandelion-dash.py` | Dashboard HTTP server: `/api/status`, `/api/options`, `/api/runs`, `/api/trigger`, `/health`; status cache (90 s), background expected-count scrapes, pre-creation expected counts, run launcher + reconciler, fail-loud trigger routing (HTTP 503 when MA is unreachable instead of guessing fill-vs-create), per-card unavailable summary |
| `dandelion_dash_lib.py` | Dashboard data layer: MA snapshots, favourites/liked counting, provider chip counts, KEXP API access paths (`kexp_play_walk`, `kexp_episode_ids`, `kexp_expected`, `kexp_options`), Dandelion scraper, disk-persisted expected-count cache, `known_months()`/`months_with_playlists()` (pre-creation support), fail-loud `existing_for()` |
| `dandelion-dash.html` | Frontend (vanilla JS): station tabs, month tabs, completeness bars, per-provider chips, red **unavailable** chips with expandable no-provider track lists, "Not built yet" placeholder cards for unbuilt months, trigger form, 30 s polling |
| `test_pr1_precreate.py` | Test suite: pre-creation expected counts (cache namespaces, persistence, None-total semantics); accepts an optional variant arg (`python3 test_pr1_precreate.py pr1`) to run against standalone branch variants |
| `test_pr2_existing.py` | Test suite: `existing_for()` outage disambiguation + trigger 503/routing; installs a tripwire so any unmocked `launch_run` fails loudly instead of spawning real runs |
| `test_pr3_cosmetic.py` | Test suite: `search_ma` phases, `_pick_best` guards, `kexp-rebuild-months` argparse |
| `kexp-rebuild-months.py` | Maintenance tool: deterministic remove-all → re-add rebuild of existing KEXP months (favourites-safe, fail-loud ordering). Months are CLI args now: `kexp-rebuild-months.py 2026-05 2026-06 [--dj "Cheryl Waters"]` (default: previous month) |
| `dandelion-cron.sh` / `kexp-cron.sh` | Monthly cron wrappers (2nd, previous month, `--resume`) |
| `dandelion-fill-cron.sh` / `kexp-fill-cron.sh` | Monthly fill wrappers (3rd, catches late matches) |

## Track matching

Search runs in two tiers per track:

1. **Library-first** (`library_only: true`) — fast check of local files/SMB share.
2. **Cross-provider fallback** (`library_only: false`, `limit: 20`) — Deezer,
   Bandcamp, Spotify, filesystem providers in one result set.

Candidates are ranked by **provider priority** (user preference order):
`local files → Deezer → Bandcamp → Spotify → other`. **BBC Sounds is excluded
entirely** — a bbc_sounds-only match is never accepted. Within the best provider
tier, scoring requires *artist confirmation* (≥1 significant shared word between any
credited artist and the result artist — diacritic-folded, stopword-filtered); title
overlap alone can't win, which killed a whole class of label-page mismatches.
Multi-artist credits (`A, B & C`) get a lead-artist query plus a title-only retry.
One retry after 2 s on timeout; timeouts and other errors are counted separately;
5 consecutive timeouts trip a circuit breaker (`MAMidRunOutage`) so a dead MA
aborts the run instead of burning its full timeout budget per remaining track.

Typical match rate: ~85–95% depending on how obscure the programming is; the rest
are genuinely absent from every connected provider.

## Unavailable tracks (no provider)

Tracks that never match any provider are not just printed and forgotten — they are
recorded to a durable store at `~/.hermes/data/unavailable-tracks.json`, keyed by
`station|month|dj|artist|title`. Each entry keeps:

- **`reason`** — why it's unavailable:
  - `no_match` — genuinely absent from every connected provider (permanent);
  - `timeout` / `api_error` — MA or network trouble during the search (retryable;
    a later fill usually resolves these).
- **`attempts` / `first_seen` / `last_seen`** — how often and how recently a build
  tried and failed to place the track.

Lifecycle/reconciliation:

- **LIVE builds** upsert every never-matched track into the store.
- **FILL runs** reconcile it: tracks that finally matched are **removed**; tracks
  still missing get their `attempts` bumped and a retryable reason **upgrades** a
  permanent one (most recent evidence wins).
- Writes are serialized across concurrent runs (`flock`) and atomic on disk
  (tmp-file + rename), so overlapping station runs can't drop each other's records.

The dashboard surfaces this per card: a red **⚠ unavailable N ▾** chip that expands
to the full track list with colour-coded reason badges (red = permanent `no_match`,
amber = retryable timeout/api error) and attempt counts. The status payload exposes
the same data as `unavailable[month][dj]`.

## Ingestion safeguards (scrape & stream hygiene)

Both ingestion paths fail soft on *individual* anomalies and fail loud on *total* ones:

| Guard | Where | Behavior |
|---|---|---|
| Zero-section scrape | `dandelion-to-ma.py` | No DJ sections at all → `exit 1` (never build an empty month) |
| **Absent-DJ detection** (PR-E) | `dandelion-to-ma.py` | A DJ header that parsed but produced **0 track rows** is dropped from the build AND reported in an `⚠️ ABSENT DJS` output block — how silent site-layout breakage presents |
| Low track count | `dandelion-to-ma.py` | Any section with <3 tracks warns loudly but continues (Dandelion publishes progressively; fill reconciles later) |
| Non-track plays | `kexp_play_walk` | `play_type != "trackplay"` and blank artist/song dropped client-side |
| Consecutive-spin dedupe | `kexp_play_walk` | Same artist+title within ≤600 s collapses inline; the window extends through repeat runs (12:00→12:03→12:06 = one spin). Interleaved different songs are kept — cross-run repeats stay owned by the global artist\|title dedupe |
| Global dedupe | `kexp_month_tracks` | Final pass: no duplicate artist\|title pair enters search |

All guards are warning-grade (except zero-sections) so a partial publish never kills a
monthly build; the dashboard's expected-vs-found bars make any shortfall visible.

## The dashboard

`GET /` serves the SPA; data endpoints:

| Endpoint | What it does |
|---|---|
| `GET /api/status?station=dandelion\|kexp` | Months → shows with tracks / liked / expected counts + provider breakdown + per-DJ unavailable summary (cached 90 s; expected counts scraped in background threads, cached 1 h in memory + persisted to disk across restarts) |
| `GET /api/options?station=…` | Chooser data: Dandelion DJ list from playlist names; KEXP all 41 programs + 106 hosts (cached 6 h) |
| `POST /api/trigger` | `{station, month, dj?, mode}` — launches the station script in its **own transient systemd unit** (`systemd-run --collect --wait`), logs to `~/.hermes/data/dandelion-runs/`, records a JSON run entry watched by a monitor thread. Modes: `auto` (fill if any playlist exists else create), `fill`, `live`, `dry`. **Fail-loud routing:** when `existing_for()` cannot reach MA it aborts with HTTP 503 instead of misreading the outage as "nothing exists". Hard cap: 2 concurrent runs (matches MA's 2-slot task queue) → HTTP 429 beyond that |
| `GET /api/runs` | Last 10 run records; zombie `running` entries whose unit died get reconciled to `died` automatically (reconciler window = lexicographic top-10 of the runs dir) |

### Pre-creation expected counts

Months where the **station source** has data but no MA playlist exists yet are no
longer invisible. `/api/status` attaches a `__precreate__` entry for them
(known via `lib.known_months()`: dandelion = months already in the expected-count
cache, kexp = current + previous month); the frontend renders a "Not built yet"
placeholder card with the expected total instead of an empty grid. Totals are
cached under `<station>-precreate:<month>` keys — only real totals; while the
underlying scrape is still running the card shows "counting…" and retries on the
next poll.

The site reflects MA edits within ~2 minutes (30 s frontend polling + 90 s server
cache) — there is no static regeneration step. Deleting/reordering tracks in the MA
UI shows up on its own.

**Security posture:** `/api/trigger` is intentionally unauthenticated (deliberate
decision for a home dashboard behind the owner's tunnel); the concurrency cap is the
only abuse brake. Do not expose this to the public internet without adding a token.

## Setup

```bash
python3 -m venv scrapling_venv
scrapling_venv/bin/pip install 'scrapling[all]'   # bare scrapling misses runtime deps
# MA_TOKEN in /root/.bashrc (Music Assistant long-lived token)
# default MA host 192.168.214.159:8095 (--ma-host to override)
```

Scripts resolve `MA_TOKEN` from the environment, falling back to parsing
`/root/.bashrc` — detached systemd units work without an exported shell env.

### Running manually

```bash
V=/root/.hermes/scripts/scrapling_venv/bin/python3

# One Dandelion DJ, dry-run first (never a bad idea)
$V dandelion-to-ma.py --month 2026-07 --dj "Mark Whitby" --dry-run

# Whole KEXP month (aggregate Midday Show playlist)
$V kexp-to-ma.py --month 2026-07 --resume

# Top up an existing month with tracks missed earlier (adds only, likes untouched)
$V kexp-to-ma.py --month 2026-06 --dj "Cheryl Waters" --fill

# Trigger through the dashboard API
curl -X POST http://127.0.0.1:9210/api/trigger \
  -H 'Content-Type: application/json' \
  -d '{"station":"kexp","month":"2026-04","dj":"Cheryl Waters","mode":"auto"}'
```

Long manual runs should go through `systemd-run --unit=<name> --collect …` — a bare
background child dies with the next service restart (learned the hard way; see
Pitfalls).

### Cron

| Job | When | Does |
|---|---|---|
| `*-cron.sh` | 2nd of month, 12:00 NZST | Build previous month (`--resume`) |
| `*-fill-cron.sh` | 3rd of month | Fill pass over the same month (catches search-timeout misses) |

Dandelion publishes tracklists "at the start of next month"; the 2nd gives a 24 h
buffer, the fill pass mops up. Cron budgets need ≥7200 s — a full Dandelion month
(~700 tracks × cross-provider search) can exceed an hour.

## Pitfalls worth knowing (all learned in production)

- **KEXP API ignores most filters.** On `/plays/`, `show=`/`date=`/`program=` are
  silently ignored (you get the newest ~300 plays regardless — this once made every
  month look identical); only `airdate_after=`/`airdate_before=`, `ordering`,
  `artist=`/`song=`/`album=` actually work. On `/shows/`, **every** filter param is
  ignored including `ordering` — the only way to reach older months is offset
  pagination through a feed that grows ~9 shows/day. Any hardcoded page cap on that
  walk is a **time bomb**: an 800-show cap worked when written and silently broke
  ~12 weeks later ("No trackplays found"). Both walks here are boundary-driven now.
- **MA `library_items` caps at 500/page** — always paginate; a single-page fetch
  blinds duplicate protection past position 500.
- **`add_playlist_tracks` takes `db_playlist_id` (int)**, not `item_id`;
  `remove_playlist_tracks` takes **positions**, not URIs (an URIs payload 500s);
  adds/removes run as background tasks (2 slots) — wait on `tasks/list` before
  verifying counts. `music/playlists/get` 500s on library playlists — use
  `playlist_tracks`.
- **Fail-loud everywhere:** every list-fetch returns `None` on API failure and
  callers exit non-zero. A silent empty set once created 20 duplicate playlists in
  one night; that class of bug is engineered out, not handled. This includes
  `existing_for()`: an MA outage during a trigger returns `None` → HTTP 503,
  never `[]` masquerading as "nothing to fill".
- **Never trust `count` fields** on KEXP paginated endpoints; page until a short page.
- **Expected-vs-found honesty:** the dashboard scrapes real per-month/per-show
  expected counts, so a card showing 45% means the playlist really is partial —
  run fill mode rather than guessing.
- **Empty ≠ absent.** An empty scrape can mean three things: the station hasn't
  published yet, the page layout changed, or the DJ genuinely had an empty show.
  Only the first is normal — that's why absent-DJ detection exists: headers with
  zero surviving rows are reported, never silently swallowed.
- **Unavailable ≠ lost.** A track missing from a playlist is either in the
  unavailable store (check the card's red chip for its reason) or genuinely absent
  from every provider. Timeouts recorded there are exactly what the monthly fill
  pass retries — the store and the fill reconcile each other.

## Maintenance tools

`kexp-rebuild-months.py` replaces the content of existing KEXP months in place
(ids/names preserved): fetch true month → search (cacheable) → remove all positions
→ verify empty → add true set → verify count, favourites checked before/after.
Use it when historical months were built by an older, wrong access path.

---

*Private homelab project. No secrets are stored in this repository — tokens live in
`/root/.hermes/.env` / `.bashrc` on the host.*
