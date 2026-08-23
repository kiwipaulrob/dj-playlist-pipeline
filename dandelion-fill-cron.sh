#!/bin/bash
# dandelion-fill-cron.sh — Scheduled Dandelion Radio FILL pass
# Runs on the 3rd of the month (day after the main playlist cron on the 2nd).
# Re-searches MISSING tracks into the previous month's EXISTING playlists
# (catches tracks the main run missed — timeouts, provider flakiness).
# Adds only — never deletes, never creates duplicates, liked tracks untouched.
#
# Usage: dandelion-fill-cron.sh [YYYY-MM]
# If no month given, defaults to previous month (same as the main cron).

MONTH="${1:-$(date -d 'last month' '+%Y-%m')}"

cd /root/.hermes/scripts

export MA_TOKEN="$(grep MA_TOKEN /root/.bashrc | head -1 | cut -d= -f2 | tr -d '"')"

# Run the fill pass
/root/.hermes/scripts/scrapling_venv/bin/python3 /root/.hermes/scripts/dandelion-to-ma.py \
  --month "$MONTH" \
  --fill \
  --delay 0.1 2>&1

echo ""
echo "Done: $(date)"
