"""
Mosaic Weather Advisory and Alert System: hand-off to Power Automate

Reads output/email.json and posts it, with the weather map page attached, to
your Power Automate flow, which sends the email from your Outlook. The flow's
address and a shared key come from GitHub secrets (MWS_FLOW_URL and
MWS_FLOW_KEY), so they never sit in the code.

Safeguards:
  - The 6 AM briefing goes out at most once per day.
  - The 9 AM, noon, 3 PM, 6 PM and 9 PM checks only send an email when
    something changed since the last email (see changes.py).
  - After a successful send, this run's results are saved in state/ so the
    next report can say what changed.

Run it with:  python src/send_email.py
"""

import base64
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
EMAIL_PATH = ROOT / "output" / "email.json"
MAP_PATH = ROOT / "output" / "weather-map.html"
RESULTS_PATH = ROOT / "output" / "results.json"
STATE_DIR = ROOT / "state"
SENT_PATH = STATE_DIR / "sent.json"
LAST_PATH = STATE_DIR / "last_results.json"


def load_sent():
    try:
        return json.loads(SENT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def main():
    url = os.environ.get("MWS_FLOW_URL", "").strip()
    key = os.environ.get("MWS_FLOW_KEY", "").strip()
    if not url or not key:
        print("MWS_FLOW_URL or MWS_FLOW_KEY is not set, so no email was sent.")
        sys.exit(1)

    mode = os.environ.get("RUN_MODE", "manual")
    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    today = datetime.fromisoformat(results["generated"]).date().isoformat()

    sent = load_sent()
    slot = os.environ.get("RUN_SLOT", "")
    if mode == "morning" and sent.get("morning") == today:
        print("The 6 AM briefing already went out today, so this run won't send another.")
        return
    if mode == "check":
        if sent.get(f"check {slot}") == today:
            print(f"The {slot} update already went out today.")
            return
        if not results.get("changes"):
            print(f"{slot} check: nothing changed since the last email, so no email.")
            return
        print(f"{slot} check: {len(results['changes'])} change(s) since the last email, sending an update.")

    payload = json.loads(EMAIL_PATH.read_text(encoding="utf-8"))
    payload["key"] = key
    # "Send to me only": the flow sends it to Adam alone, and it doesn't count as a sent report
    test = os.environ.get("DELIVERY") == "Send to me only" and mode == "manual"
    payload["audience"] = "me" if test else "all"
    if test:
        payload["subject"] = "[Test] " + payload["subject"]
    stamp = results["flag_days"][0]["date"]
    if MAP_PATH.exists():
        payload["map_name"] = f"MWS Weather Map {stamp}.html"
        payload["map_b64"] = base64.b64encode(MAP_PATH.read_bytes()).decode("ascii")
    else:
        payload["map_name"] = f"MWS note {stamp}.txt"
        payload["map_b64"] = base64.b64encode(b"The weather map could not be built for this run.").decode("ascii")

    last = None
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=90)
        except requests.ConnectionError as e:
            last = str(e)  # never reached the flow, safe to retry
        except requests.Timeout:
            # The flow may have received it and still be sending, so don't
            # risk a duplicate email by retrying.
            print("Power Automate didn't answer in time. The email may still arrive; not retrying.")
            sys.exit(1)
        else:
            if 200 <= r.status_code < 300:
                print(f"Handed off to Power Automate (HTTP {r.status_code}): {payload['subject']}")
                if test:
                    print("Test send: not saved as the last report, so the next check still compares with the last real email.")
                    return
                STATE_DIR.mkdir(exist_ok=True)
                if mode in ("morning", "check"):
                    sent["morning" if mode == "morning" else f"check {slot}"] = today
                    SENT_PATH.write_text(json.dumps(sent), encoding="utf-8")
                LAST_PATH.write_text(json.dumps(results), encoding="utf-8")
                return
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code < 500 and r.status_code != 429:
                break  # a wrong key or URL won't fix itself
        time.sleep(10 * (attempt + 1))
    print(f"Power Automate did not accept the email. Last response: {last}")
    sys.exit(1)


if __name__ == "__main__":
    main()
