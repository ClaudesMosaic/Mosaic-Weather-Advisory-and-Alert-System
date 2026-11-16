"""
Mosaic Weather Advisory and Alert System: hand-off to Power Automate

Reads output/email.json and posts it, with the weather map page attached, to
your Power Automate flow, which sends the email from your Outlook. The flow's address and a shared key come from
GitHub secrets (MWS_FLOW_URL and MWS_FLOW_KEY), so they never sit in the code.

Run it with:  python src/send_email.py
"""

import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
EMAIL_PATH = ROOT / "output" / "email.json"
MAP_PATH = ROOT / "output" / "weather-map.html"
RESULTS_PATH = ROOT / "output" / "results.json"


def main():
    url = os.environ.get("MWS_FLOW_URL", "").strip()
    key = os.environ.get("MWS_FLOW_KEY", "").strip()
    if not url or not key:
        print("MWS_FLOW_URL or MWS_FLOW_KEY is not set, so no email was sent.")
        sys.exit(1)

    # The noon run only sends when there's at least one flag in the next 3 days
    if os.environ.get("RUN_MODE") == "noon":
        results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        flagged = any(v["status"] == "flag" for d in results["flag_days"] for v in d["locations"].values()) or \
            any(x["status"] == "flag" for d in results["flag_days"] for x in d["runs"])
        if not flagged:
            print("Noon run: no flags in the next 3 days, so no email today.")
            return

    payload = json.loads(EMAIL_PATH.read_text(encoding="utf-8"))
    payload["key"] = key

    # Attach the weather map page, if it was built
    if MAP_PATH.exists():
        stamp = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))["flag_days"][0]["date"]
        payload["map_name"] = f"MWS Weather Map {stamp}.html"
        payload["map_b64"] = base64.b64encode(MAP_PATH.read_bytes()).decode("ascii")
    else:
        payload["map_name"], payload["map_b64"] = "", ""

    last = None
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=60)
            if 200 <= r.status_code < 300:
                print(f"Handed off to Power Automate (HTTP {r.status_code}): {payload['subject']}")
                return
            last = f"HTTP {r.status_code}: {r.text[:300]}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(10 * (attempt + 1))
    print(f"Power Automate did not accept the email. Last response: {last}")
    sys.exit(1)


if __name__ == "__main__":
    main()
