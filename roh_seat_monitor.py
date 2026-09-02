#!/usr/bin/env python3
"""
ROH Seat Availability Monitor
------------------------------
Polls the Royal Opera House seatmap API for a given performance and
alerts you (via ntfy.sh push notification, free & no signup) when the
number of available seats increases, or when specific seats appear.

SETUP:
1. pip install requests
2. Adjust CONFIG below.
3. Run once with --inspect to print the raw JSON structure so you can
   confirm the field names used to detect "available" seats.
4. Then run normally: python monitor.py
   (or schedule it with cron / Task Scheduler to run every N minutes)

NOTIFICATIONS:
Uses ntfy.sh — install the ntfy app (iOS/Android) or just visit
https://ntfy.sh/<your-topic> in a browser, subscribe to a topic name
of your choosing, and put that same topic name in CONFIG below.
No account needed. If you'd rather use email, see the send_email()
stub near the bottom.
"""

import requests
import json
import os
import time
import sys
from pathlib import Path

# ----------------- CONFIG -----------------
URL = "https://www.rbo.org.uk/api/v2-proxy/TXN/Performances/74463/Seats?constituentId=0&modeOfSaleId=10&performanceId=74463"

# This endpoint returned data without a login cookie, so auth isn't
# needed. If that ever changes (HTTP 401/403), set a COOKIE_HEADER
# environment variable (or a GitHub Actions secret of the same name)
# with the value copied from DevTools -> Network -> this request ->
# Headers -> Cookie.
COOKIE_HEADER = os.environ.get("COOKIE_HEADER", "")

# A seat is treated as "available" when HoldCodeId == this value.
# Observed: HoldCodeId 0 = no hold (available), 460 = held/sold/blocked.
# If alerts seem wrong once real seats free up, tell me and we'll
# adjust this against a fresh sample.
AVAILABLE_HOLD_CODE_ID = 0

# ntfy.sh topic name -- pick something unique/hard to guess, e.g.
# "roh-perf74463-desmond-x9k2". Set via NTFY_TOPIC env var / GitHub
# secret so you don't have to commit it into the repo.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "CHANGE-ME-roh-alert")

POLL_SECONDS = 120  # only used in --loop mode (local/manual runs)
STATE_FILE = Path(__file__).parent / "last_state.json"
# -------------------------------------------


def fetch_seats():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SeatMonitor/1.0)",
        "Accept": "application/json",
    }
    if COOKIE_HEADER:
        headers["Cookie"] = COOKIE_HEADER

    resp = requests.get(URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def send_notification(message):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": "ROH Seat Alert"},
            timeout=10,
        )
    except Exception as e:
        print(f"[warn] failed to send notification: {e}")


def extract_available_seats(data):
    """
    Returns a dict of {seat_id: "RowLetter SeatNumber"} for every seat
    currently available (HoldCodeId == AVAILABLE_HOLD_CODE_ID).
    """
    seats = data if isinstance(data, list) else data.get("Seats", data)
    available = {}
    for s in seats:
        if not s.get("IsSeat"):
            continue
        if s.get("HoldCodeId") == AVAILABLE_HOLD_CODE_ID:
            label = f"Row {s.get('SeatRow')}, Seat {s.get('SeatNumber')}"
            available[s["Id"]] = label
    return available


def check_once():
    """Single check-and-alert pass. This is what GitHub Actions runs each
    time the scheduled workflow fires — check, alert if needed, exit."""
    prev_ids = set()
    first_run = not STATE_FILE.exists()
    if STATE_FILE.exists():
        try:
            prev_ids = set(json.loads(STATE_FILE.read_text()).get("seat_ids", []))
        except Exception:
            prev_ids = set()

    data = fetch_seats()
    available = extract_available_seats(data)
    current_ids = set(available.keys())
    print(f"[{time.strftime('%H:%M:%S')}] available seats detected: {len(current_ids)}")

    new_ids = current_ids - prev_ids
    if new_ids and not first_run:
        labels = ", ".join(available[i] for i in sorted(new_ids))
        send_notification(
            f"{len(new_ids)} new seat(s) available for performance 74463: {labels}"
        )
        print(f"  -> ALERT sent: {labels}")
    elif first_run:
        send_notification(
            f"Monitor is live for performance 74463. Baseline: {len(current_ids)} seat(s) "
            f"currently available. You'll be alerted when new ones open up."
        )
        print("  -> first run: baseline established, confirmation notification sent")

    STATE_FILE.write_text(json.dumps({"seat_ids": list(current_ids)}))


def main():
    if "--inspect" in sys.argv:
        data = fetch_seats()
        print(json.dumps(data, indent=2)[:3000])
        print("\n... (truncated if longer). Use this to confirm field names.")
        return

    if "--loop" in sys.argv:
        print("Starting continuous monitor. Press Ctrl+C to stop.")
        while True:
            try:
                check_once()
            except requests.HTTPError as e:
                print(f"[error] HTTP error: {e} — you may need to be logged in (set COOKIE_HEADER)")
            except Exception as e:
                print(f"[error] {e}")
            time.sleep(POLL_SECONDS)
        return

    # Default: single check-and-exit (used by GitHub Actions)
    check_once()


if __name__ == "__main__":
    main()