#!/bin/bash
# kexp-fill-cron.sh — Scheduled KEXP Midday Show FILL pass
# Runs on the 3rd of the month (day after kexp-cron.sh). Re-searches MISSING
# tracks into the previous month's existing KEXP playlist (adds only).
# Usage: kexp-fill-cron.sh [YYYY-MM]

MONTH="${1:-$(date -d 'last month' '+%Y-%m')}"

cd /root/.hermes/scripts

export MA_TOKEN="$(grep MA_TOKEN /root/.bashrc | head -1 | cut -d= -f2 | tr -d '"')"

/root/.hermes/scripts/scrapling_venv/bin/python3 /root/.hermes/scripts/kexp-to-ma.py \
  --month "$MONTH" \
  --fill \
  --delay 0.1 2>&1

echo ""
echo "Done: $(date)"
