#!/bin/bash
# kexp-cron.sh — Scheduled KEXP Midday Show monthly playlist generator
# Called by cron on the 2nd of the month. Creates the previous month's
# aggregate playlist: 'KEXP - {Month YYYY} - The Midday Show'.
# Usage: kexp-cron.sh [YYYY-MM]  (defaults to previous month)

MONTH="${1:-$(date -d 'last month' '+%Y-%m')}"

cd /root/.hermes/scripts

export MA_TOKEN="$(grep MA_TOKEN /root/.bashrc | head -1 | cut -d= -f2 | tr -d '"')"

/root/.hermes/scripts/scrapling_venv/bin/python3 /root/.hermes/scripts/kexp-to-ma.py \
  --month "$MONTH" \
  --resume \
  --delay 0.1 2>&1

echo ""
echo "Done: $(date)"
