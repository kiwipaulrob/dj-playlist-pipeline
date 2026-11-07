#!/bin/bash
# dj-pipeline-github-sync.sh — one-way sync of DJ pipeline files to GitHub.
# Source of truth: /root/.hermes/scripts/<file>  ->  kiwipaulrob/dj-playlist-pipeline
# Runs as a no_agent Hermes cron job: prints NOTHING when clean (watchdog
# pattern — empty stdout sends no message), prints a summary on sync/failure.
#
# Safety rails:
#   * explicit allowlist only — never syncs the whole scripts dir (it holds
#     private homelab automation that must not go to the public repo)
#   * secret scan blocks the commit if a key/token-shaped value appears
#   * fetch+rebase first; any rebase conflict aborts WITHOUT pushing
#   * never force-pushes
# Usage: dj-pipeline-github-sync.sh [--check]  (--check reports, never writes)

set -u
MODE="${1:-sync}"

SRC="/root/.hermes/scripts"
REPO_ROOT="/root/.hermes/profiles/music-maintainer/repos"
REPO="$REPO_ROOT/dj-playlist-pipeline"
REMOTE="https://github.com/kiwipaulrob/dj-playlist-pipeline.git"
BRANCH="main"

FILES=(
  dandelion-dash.html
  dandelion-dash.py
  dandelion_dash_lib.py
  dandelion-email-summary.py
  dandelion-to-ma.py
  dj_playlist_local_sweep.py
  kexp-rebuild-all.py
  kexp-rebuild-june-july.py
  kexp-rebuild-months.py
  kexp-rebuild-watch.py
  kexp-to-ma.py
  ma-merge-dandelion-duplicates.py
  ma_playlist_lib.py
)

fail() { echo "dj-sync FAILED: $1"; exit 1; }

[ -d "$SRC" ] || fail "source dir missing: $SRC"
command -v git >/dev/null || fail "git not found"

if [ ! -d "$REPO/.git" ]; then
  mkdir -p "$REPO_ROOT"
  git clone --quiet "$REMOTE" "$REPO" 2>&1 || fail "clone failed"
  git -C "$REPO" config user.name hermes-agent
  git -C "$REPO" config user.email hermes@robertsons.cloud
fi

# Pick up remote changes first so we never clobber them.
git -C "$REPO" fetch --quiet origin "$BRANCH" 2>&1 \
  || fail "fetch failed (network or auth)"
git -C "$REPO" pull --quiet --rebase origin "$BRANCH" 2>&1 \
  || { git -C "$REPO" rebase --abort >/dev/null 2>&1; fail "rebase conflict — remote changed, manual merge needed"; }

CHANGED=()
for f in "${FILES[@]}"; do
  if [ ! -f "$SRC/$f" ]; then
    [ "$MODE" = "--check" ] && echo "dj-sync NOTE: allowlisted file missing from source: $f"
    continue
  fi
  if ! diff -q "$SRC/$f" "$REPO/$f" >/dev/null 2>&1; then
    CHANGED+=("$f")
  fi
done

# A file deleted from the repo upstream that still exists locally counts too.
for f in "${FILES[@]}"; do
  if [ -f "$SRC/$f" ] && [ ! -e "$REPO/$f" ]; then
    case " ${CHANGED[*]} " in *" $f "*) ;; *) CHANGED+=("$f");; esac
  fi
done

if [ "${#CHANGED[@]}" -eq 0 ]; then
  [ "$MODE" = "--check" ] && echo "dj-sync: clean — GitHub matches local."
  exit 0
fi

if [ "$MODE" = "--check" ]; then
  echo "dj-sync: would sync ${#CHANGED[@]} file(s): ${CHANGED[*]}"
  exit 0
fi

for f in "${CHANGED[@]}"; do cp "$SRC/$f" "$REPO/$f" || fail "copy failed for $f"; done

# Secret gate: block anything that looks like a hardcoded credential.
BLOCKERS=$(grep -lniE "bearer [A-Za-z0-9_-]{10,}|api[_-]?key\s*=\s*['\"][^'\"]+|password\s*=\s*['\"][^'\"]+|token\s*=\s*['\"][A-Za-z0-9_-]{12,}|-----BEGIN .*PRIVATE KEY" "${CHANGED[@]/#/$REPO/}" 2>/dev/null || true)
if [ -n "$BLOCKERS" ]; then
  git -C "$REPO" checkout -- . >/dev/null 2>&1
  fail "secret-shaped value found, commit blocked: $BLOCKERS"
fi

git -C "$REPO" add -A >/dev/null 2>&1
if git -C "$REPO" diff --cached --quiet; then
  echo "dj-sync: files differed but nothing staged (mode-only change?), skipping."
  exit 0
fi
git -C "$REPO" commit --quiet -m "Sync local DJ pipeline ($(date '+%F')): ${CHANGED[*]}" \
  || fail "commit failed"
git -C "$REPO" push --quiet origin "$BRANCH" 2>&1 \
  || fail "push failed (network or auth)"

echo "dj-sync: pushed ${#CHANGED[@]} file(s) to dj-playlist-pipeline: ${CHANGED[*]}"
