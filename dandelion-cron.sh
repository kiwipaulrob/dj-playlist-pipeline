#!/bin/bash
# dandelion-cron.sh — Scheduled Dandelion Radio playlist generator
# Called by cron. Scrapes the previous month's tracklists and creates
# MA playlists for each DJ show.
#
# Usage: dandelion-cron.sh [YYYY-MM]
# If no month given, defaults to previous month.
# Output is delivered by the cron system (deliver: email).

MONTH="${1:-$(date -d 'last month' '+%Y-%m')}"

cd /root/.hermes/scripts

export MA_TOKEN="$(grep MA_TOKEN /root/.bashrc | head -1 | cut -d= -f2 | tr -d '"')"

# Run the scraper
# M3 (15 Aug 2026): --resume skips DJ shows whose canonical playlist already
# exists (e.g. partial mid-month runs) instead of creating "(2)" duplicates.
/root/.hermes/scripts/scrapling_venv/bin/python3 /root/.hermes/scripts/dandelion-to-ma.py \
  --month "$MONTH" \
  --resume \
  --delay 0.1 2>&1

echo ""
echo "Done: $(date)"
