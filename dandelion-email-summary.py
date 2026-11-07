#!/usr/bin/env python3
"""Email a summary of the radio playlist dashboard (per-provider counts).

Reads live /api/status from the local dashboard and sends a dark-themed
HTML summary via SMTP using the gateway's Gmail credentials.
"""
import json
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

EMAIL_FROM = "twikikiwi@gmail.com"
EMAIL_TO = "paularobertson@gmail.com"
DASH = "http://127.0.0.1:9210"


def load_env():
    env = {}
    for line in open("/root/.hermes/.env"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            v = v.strip()
            # Strip wrapping quotes - .env values may be stored quoted (535 bug class)
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                v = v[1:-1]
            env[k.strip()] = v
    return env


def get_status(station):
    with urllib.request.urlopen(f"{DASH}/api/status?station={station}", timeout=60) as r:
        return json.loads(r.read())


def fmt_provs(provs):
    if not provs:
        return '<span style="color:#64748b">—</span>'
    parts = []
    for p, n in provs.items():
        color = "#4ade80" if p == "Bandcamp" else "#60a5fa" if p == "Spotify" else "#fb923c" if p == "Deezer" else "#c084fc" if p == "BBC Sounds" else "#e0e0e0"
        parts.append(f'<span style="background:#0f3460;color:{color};border-radius:8px;padding:1px 7px;font-size:11px;margin-right:3px">{p} <b>{n}</b></span>')
    return " ".join(parts)


def card_rows(station):
    st = get_status(station)
    months = st.get("months", {})
    rows = []
    for m in sorted(months, reverse=True):
        for dj, e in sorted(months[m].items()):
            rows.append(
                f'<tr><td style="padding:8px 10px;border-bottom:1px solid #0f3460;color:#e0e0e0">{m}</td>'
                f'<td style="padding:8px 10px;border-bottom:1px solid #0f3460;color:#e0e0e0">{dj}</td>'
                f'<td style="padding:8px 10px;border-bottom:1px solid #0f3460;color:#e0e0e0;text-align:center">{e.get("tracks", 0)}</td>'
                f'<td style="padding:8px 10px;border-bottom:1px solid #0f3460">{fmt_provs(e.get("providers", {}))}</td>'
                f'<td style="padding:8px 10px;border-bottom:1px solid #0f3460;color:#f87171;text-align:center">{e.get("liked", 0)}</td></tr>')
    return rows, st.get("favourites_total", 0)


def main():
    env = load_env()
    if not env.get("EMAIL_PASSWORD"):
        print("ERROR: EMAIL_PASSWORD not found", file=sys.stderr)
        sys.exit(1)

    d_rows, d_fav = card_rows("dandelion")
    k_rows, k_fav = card_rows("kexp")
    nz = timezone(timedelta(hours=12))
    now = datetime.now(nz).strftime("%Y-%m-%d %H:%M %Z")

    html = f"""<html><body style="margin:0;padding:20px;background:#1a1a2e;font-family:Arial,Helvetica,sans-serif;color:#e0e0e0">
<h2 style="color:#e94560;margin:0 0 4px">📻 Radio Playlist Dashboard</h2>
<div style="color:#64748b;font-size:12px;margin-bottom:16px">Generated {now} · live from djs.robertsons.cloud</div>

<div style="background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:14px;margin-bottom:16px">
<b style="color:#e94560">Dandelion Radio</b> — <span style="color:#64748b">{len(d_rows)} playlists · {d_fav} liked songs in MA</span>
<table style="width:100%;border-collapse:collapse;margin-top:8px">
<tr><th style="text-align:left;padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">Month</th>
<th style="text-align:left;padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">DJ</th>
<th style="padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">Tracks</th>
<th style="text-align:left;padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">Providers</th>
<th style="padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">♥</th></tr>
{''.join(d_rows)}
</table></div>

<div style="background:#16213e;border:1px solid #0f3460;border-radius:10px;padding:14px;margin-bottom:16px">
<b style="color:#e94560">KEXP</b> — <span style="color:#64748b">{len(k_rows)} playlists · {k_fav} liked songs in MA</span>
<table style="width:100%;border-collapse:collapse;margin-top:8px">
<tr><th style="text-align:left;padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">Month</th>
<th style="text-align:left;padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">Show</th>
<th style="padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">Tracks</th>
<th style="text-align:left;padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">Providers</th>
<th style="padding:6px 10px;background:#0f3460;color:#e94560;font-size:12px">♥</th></tr>
{''.join(k_rows)}
</table></div>

<div style="background:#1e3a5f;border-left:4px solid #60a5fa;border-radius:6px;padding:10px 14px;font-size:12px;color:#cbd5e1">
<b style="color:#60a5fa">What's new</b> — per-provider song counts now shown on every playlist card and in the KEXP station header (follows the show/DJ chooser). Provider counts derive from MA provider_mappings; a track matched on two providers counts for both. Dashboard: <a href="https://djs.robertsons.cloud" style="color:#60a5fa">djs.robertsons.cloud</a>
</div>
<div style="color:#64748b;font-size:11px;margin-top:14px">— Hermes Agent · dandelion-dash</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Hermes Bot <{EMAIL_FROM}>"
    msg["To"] = EMAIL_TO
    msg["Subject"] = "Radio Playlist Dashboard — per-provider counts live"
    msg.attach(MIMEText("View with HTML-capable client", "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP(env.get("EMAIL_SMTP_HOST", "smtp.gmail.com"),
                      int(env.get("EMAIL_SMTP_PORT", 587)), timeout=30) as s:
        s.starttls()
        s.login(EMAIL_FROM, env["EMAIL_PASSWORD"])
        s.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())
    print(f"sent to {EMAIL_TO}: dandelion {len(d_rows)} playlists, kexp {len(k_rows)} playlists")


if __name__ == "__main__":
    main()
