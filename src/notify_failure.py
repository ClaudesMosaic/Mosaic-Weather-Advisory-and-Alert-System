"""
Mosaic Weather Advisory and Alert System: failure alert

If any step of a run fails, the workflow calls this to send a short
"the weather run failed" email through the same Power Automate flow, with a
link to the run so you can see which step broke.

Run it with:  python src/notify_failure.py
"""

import base64
import os
import sys

import requests


def main():
    url = os.environ.get("MWS_FLOW_URL", "").strip()
    key = os.environ.get("MWS_FLOW_KEY", "").strip()
    if not url or not key:
        print("Flow secrets missing, so no failure alert could be sent.")
        sys.exit(0)
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
    mode = os.environ.get("RUN_MODE", "")
    html = (
        "<div style=\"font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#1c2620\">"
        "<div style=\"font-size:20px;font-weight:bold;color:#c23b2a\">The Mosaic Weather System run failed</div>"
        f"<p>The {mode or 'scheduled'} run didn't finish, so this report may be missing or incomplete.</p>"
        f"<p><a href=\"{run_url}\">Open the run on GitHub</a> and click the step with the red X to see what went wrong. "
        "The National Weather Service being briefly down is the usual cause, and the next run normally works.</p></div>")
    note = "The weather run failed. See the link in the email for details.".encode()
    payload = {"subject": "❌ MWS: the weather run failed", "html": html, "key": key,
               "map_name": "MWS run failed.txt", "map_b64": base64.b64encode(note).decode("ascii")}
    try:
        r = requests.post(url, json=payload, timeout=60)
        print(f"Failure alert sent (HTTP {r.status_code}).")
    except requests.RequestException as e:
        print(f"Couldn't send the failure alert: {e}")


if __name__ == "__main__":
    main()
