#!/usr/bin/env python3
"""
Xbox Series X (Refurbished, en-GB) stock checker.

Runs on: GitHub Actions runner (server), or locally for testing (client).
Renders the page in headless Chromium (the page is JS-driven), inspects the
purchase buttons/text inside <main>, and notifies on an OUT -> IN transition.

Env vars (all optional):
  XBOX_URL      page to check (defaults to the refurbished Series X page)
  STATE_FILE    where the last status is stored (default state/xbox_stock.json)
  NTFY_TOPIC2    ntfy.sh topic for push notifications
  WEBHOOK_URL   Teams/Slack incoming webhook URL
  NOTIFY_ALWAYS "1" to notify on every IN_STOCK run, not just transitions
"""
import json
import os
import pathlib
import re
import sys
import urllib.request
from datetime import datetime, timezone

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = os.getenv("XBOX_URL", "https://www.xbox.com/en-GB/consoles/xbox-series-x/refurbished")
STATE_FILE = pathlib.Path(os.getenv("STATE_FILE", "state/xbox_stock.json"))
NTFY_TOPIC2 = os.getenv("NTFY_TOPIC2")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
NOTIFY_ALWAYS = os.getenv("NOTIFY_ALWAYS") == "1"
ARTIFACTS = pathlib.Path("artifacts")

# Button/link text that means "you can buy it"
BUY_PATTERNS = re.compile(r"\b(add to (cart|bag|basket)|buy now|buy refurbished|order now)\b", re.I)
# Page text that means "you can't"
OOS_PATTERNS = re.compile(r"\b(out of stock|sold out|currently unavailable|temporarily unavailable|no longer available|coming soon)\b", re.I)


def fetch_page():
    """Render the page and return (main_text, enabled_buy_buttons, html)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            locale="en-GB",
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60_000)

        # Dismiss Microsoft cookie banner if present
        for label in ("Accept", "Accept all"):
            try:
                page.get_by_role("button", name=label, exact=True).first.click(timeout=3_000)
                break
            except PWTimeout:
                pass
            except Exception:
                pass

        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            pass  # analytics often keeps the network busy; carry on

        scope = page.locator("main") if page.locator("main").count() else page.locator("body")
        main_text = scope.inner_text(timeout=10_000)

        buy_buttons = []
        for el in scope.locator("a, button").all():
            try:
                text = (el.inner_text(timeout=1_000) or el.get_attribute("aria-label") or "").strip()
            except Exception:
                continue
            if not text or not BUY_PATTERNS.search(text):
                continue
            disabled = el.get_attribute("aria-disabled") == "true" or el.get_attribute("disabled") is not None
            if not disabled and el.is_visible():
                buy_buttons.append(" ".join(text.split()))

        ARTIFACTS.mkdir(exist_ok=True)
        html = page.content()
        page.screenshot(path=str(ARTIFACTS / "page.png"), full_page=True)
        (ARTIFACTS / "page.html").write_text(html, encoding="utf-8")
        browser.close()
        return main_text, buy_buttons, html


SOLD_OUT = re.compile(r"\bsold out\b", re.I)
# Proves the product section actually rendered (not a block/error page)
PAGE_RENDERED = re.compile(r"series x", re.I)


def classify(main_text, buy_buttons):
    sold_out = len(SOLD_OUT.findall(main_text))
    oos_hits = sorted({m.group(0).lower() for m in OOS_PATTERNS.finditer(main_text)})

    if buy_buttons:
        # A live buy button wins even if another variant still says sold out
        return "IN_STOCK", {"buy_buttons": buy_buttons, "sold_out_count": sold_out}
    if sold_out or oos_hits:
        return "OUT_OF_STOCK", {"sold_out_count": sold_out, "oos_text": oos_hits}
    if PAGE_RENDERED.search(main_text):
        # Page loaded fine and "Sold out" has gone - treat as in stock
        return "IN_STOCK", {"buy_buttons": ["(no 'Sold out' text on page)"]}
    return "UNKNOWN", {"note": "Page didn't render product content - check artifacts/page.png"}


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def notify(title, message):
    if NTFY_TOPIC2:
        try:
            post(
                f"https://ntfy.sh/{NTFY_TOPIC2}",
                message.encode(),
                {"Title": title, "Priority": "high", "Tags": "video_game", "Click": URL},
            )
        except Exception as e:
            print(f"ntfy failed: {e}", file=sys.stderr)
    if WEBHOOK_URL:
        try:
            body = json.dumps({"text": f"*{title}*\n{message}\n{URL}"}).encode()
            post(WEBHOOK_URL, body, {"Content-Type": "application/json"})
        except Exception as e:
            print(f"webhook failed: {e}", file=sys.stderr)


def write_outputs(status, details):
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"status={status}\n")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(f"## Xbox Series X refurbished: **{status}**\n\n")
            f.write(f"```json\n{json.dumps(details, indent=2)}\n```\n")


def main():
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if os.getenv("TEST_NOTIFY") == "1":
        if not (NTFY_TOPIC2 or WEBHOOK_URL):
            print("TEST_NOTIFY set but no NTFY_TOPIC2 or WEBHOOK_URL configured", file=sys.stderr)
            return 1
        notify("Xbox stock checker: test", f"Test notification sent at {now}. If you can read this, alerts work.")
        print("Test notification sent")
        return 0
    try:
        main_text, buy_buttons, _ = fetch_page()
        status, details = classify(main_text, buy_buttons)
    except Exception as e:
        status, details = "ERROR", {"error": str(e)}

    prev = load_state().get("status")
    print(f"[{now}] status={status} (previous={prev}) details={json.dumps(details)}")

    if status == "IN_STOCK" and (prev != "IN_STOCK" or NOTIFY_ALWAYS):
        notify("Xbox Series X refurbished IN STOCK", f"Buttons: {', '.join(details['buy_buttons'])}")
    elif status == "OUT_OF_STOCK" and prev == "IN_STOCK":
        notify("Xbox Series X refurbished sold out again", "Back to out of stock.")

    # Don't overwrite a good state with a transient error/unknown
    if status in ("IN_STOCK", "OUT_OF_STOCK"):
        save_state({"status": status, "checked_at": now, "details": details})

    write_outputs(status, details)
    return 1 if status == "ERROR" else 0


if __name__ == "__main__":
    sys.exit(main())