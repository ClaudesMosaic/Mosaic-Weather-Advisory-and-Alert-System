"""
Mosaic Weather Advisory and Alert System: email builder

Reads output/results.json and config/network.yaml and writes:
  output/email.html   the email body (open it in a browser to preview)
  output/email.json   subject + body together, for Power Automate to send

Built with tables and inline styles, since that's what Outlook renders reliably.

Run it with:  python src/build_email.py
"""

import json
import os
from datetime import datetime
from html import escape
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "network.yaml"
RESULTS_PATH = ROOT / "output" / "results.json"
HTML_PATH = ROOT / "output" / "email.html"
JSON_PATH = ROOT / "output" / "email.json"

# Mosaic colors and status colors
GREEN = "#4d8723"
INK = "#1c2620"
MUTED = "#5b6a60"
LINE = "#e1e6df"
ZEBRA = "#f3f6f1"
FLAG_BG, FLAG_BAR, FLAG_TXT = "#fbe5e1", "#c23b2a", "#9b2c1e"
WATCH_BG, WATCH_BAR = "#fbf0d8", "#b77a0e"
CLEAR_BG, CLEAR_TXT = "#e7f1dc", "#33601a"
OUTLOOK = {"a": ("#f6b46a", "Potential hazard"), "y": ("#f4e07a", "Worth watching"), "g": ("#c6e5a6", "Nothing notable")}
STATE_NAMES = {"VA": "Virginia", "MD": "Maryland", "NC": "North Carolina", "DC": "Washington, DC", "DE": "Delaware", "WV": "West Virginia"}
FONT = "font-family:Arial,Helvetica,sans-serif;"


def e(s):
    return escape(str(s)) if s is not None else ""


def temp(v):
    return "–" if v is None else f"{v}°"


# ----------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------
def collect_items(flag_days):
    """Every flag and heads-up for days 1 to 3, locations first, then runs."""
    flags, watches = [], []
    for day in flag_days:  # already in date order, so the lists stay in date order
        for v in day["locations"].values():
            for f in v["flags"]:
                row = (day["label"], v["name"], f"{f['rule']} ({f['when']}). {f['text']}")
                (flags if f["level"] == "flag" else watches).append(row)
        for run in day["runs"]:
            if run["status"] == "clear":
                continue
            bad = [p for p in run["points"] if p["status"] == run["status"]]
            stops = "; ".join(f"{p['name']}, {p['window']}: {', '.join(p['reasons']).lower() or 'see forecast'}"
                              for p in bad)
            row = (day["label"], f"{run['name']} run", f"Leaves {clock_str(run['depart'])}. {stops}.")
            (flags if run["status"] == "flag" else watches).append(row)
    return flags, watches


def clock_str(hm):
    t = datetime.strptime(hm, "%H:%M")
    return t.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def banner(flags, watches):
    if flags:
        rows = "".join(item_row(*r, FLAG_BG, "#f0c9c2") for r in flags)
        rows += "".join(item_row(*r, WATCH_BG, "#efdcae") for r in watches)
        n = len(flags)
        head = f"&#9888;&#65039; {n} weather flag{'s' if n != 1 else ''} in the next 3 days"
        sub = f"<div style=\"{FONT}font-size:13px;color:{MUTED};margin:2px 0 8px\">Plus {len(watches)} heads-up{'s' if len(watches) != 1 else ''}, shown in yellow.</div>" if watches else ""
        return (f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse\"><tr>"
                f"<td width=\"5\" style=\"background:{FLAG_BAR}\"></td><td style=\"background:{FLAG_BG};padding:12px 14px\">"
                f"<div style=\"{FONT}font-size:17px;font-weight:bold;color:{FLAG_TXT}\">{head}</div>{sub}"
                f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;table-layout:fixed\">"
                f"<colgroup><col width=\"95\"><col width=\"150\"><col></colgroup>{rows}</table></td></tr></table>")
    if watches:
        rows = "".join(item_row(*r, WATCH_BG, "#efdcae") for r in watches)
        n = len(watches)
        return (f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse\"><tr>"
                f"<td width=\"5\" style=\"background:{WATCH_BAR}\"></td><td style=\"background:{WATCH_BG};padding:12px 14px\">"
                f"<div style=\"{FONT}font-size:17px;font-weight:bold;color:{INK}\">&#9989; No weather flags in the next 3 days</div>"
                f"<div style=\"{FONT}font-size:13px;color:{MUTED};margin:2px 0 8px\">{n} heads-up{'s' if n != 1 else ''} worth knowing about:</div>"
                f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;table-layout:fixed\">"
                f"<colgroup><col width=\"95\"><col width=\"150\"><col></colgroup>{rows}</table></td></tr></table>")
    return (f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse\"><tr>"
            f"<td width=\"5\" style=\"background:{GREEN}\"></td><td style=\"background:{CLEAR_BG};padding:12px 14px;{FONT}font-size:17px;font-weight:bold;color:{CLEAR_TXT}\">"
            f"&#9989; No weather flags for the next 3 days</td></tr></table>")


def item_row(day, who, text, bg, rule):
    td = f"style=\"{FONT}font-size:14px;color:{INK};padding:6px 8px 6px 0;border-bottom:1px solid {rule};vertical-align:top\""
    return f"<tr><td width=\"95\" {td}><b>{e(day)}</b></td><td width=\"150\" {td}><b>{e(who)}</b></td><td {td}>{e(text)}</td></tr>"


def forecast_tables(day, cfg):
    by_state = {}
    for key, loc in cfg["locations"].items():
        by_state.setdefault(loc["state"], []).append(key)
    th = f"style=\"{FONT}font-size:13px;font-weight:bold;color:#ffffff;background:{GREEN};padding:6px 8px;text-align:left\""
    thr = th.replace("text-align:left", "text-align:right")
    out = []
    for state, keys in by_state.items():
        rows = []
        for i, k in enumerate(keys):
            v = day["locations"].get(k)
            if not v:
                continue
            s = v["summary"]
            bg = FLAG_BG if v["status"] == "flag" else WATCH_BG if v["status"] == "watch" else (ZEBRA if i % 2 else "#ffffff")
            mark = "&#9888;&#65039; " if v["status"] == "flag" else ""
            td = f"style=\"{FONT}font-size:14px;color:{INK};padding:6px 8px;border-bottom:1px solid {LINE};background:{bg}\""
            tdr = td.replace("padding:6px 8px", "padding:6px 8px;text-align:right")
            rows.append(
                f"<tr><td width=\"170\" {td}>{mark}<b>{e(v['name'])}</b></td><td width=\"165\" {td}>{e(s['conditions'])}</td>"
                f"<td width=\"115\" {tdr}>{temp(s['high'])} / {temp(s['low'])}</td><td width=\"75\" {tdr}>{s['pop']}%</td>"
                f"<td width=\"75\" {tdr}>{s['rain_in']:.2f}\"</td><td width=\"80\" {tdr}>{s['snow_in']:.1f}\"</td></tr>")
        out.append(
            f"<div style=\"{FONT}font-size:15px;font-weight:bold;color:{INK};margin:18px 0 6px\">{e(STATE_NAMES.get(state, state))}</div>"
            f"<table role=\"presentation\" width=\"680\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;table-layout:fixed;width:100%;max-width:680px\">"
            f"<colgroup><col width=\"170\"><col width=\"165\"><col width=\"115\"><col width=\"75\"><col width=\"75\"><col width=\"80\"></colgroup>"
            f"<tr><th width=\"170\" {th}>Location</th><th width=\"165\" {th}>Conditions</th><th width=\"115\" {thr}>High / Low</th>"
            f"<th width=\"75\" {thr}>Precip</th><th width=\"75\" {thr}>Rain</th><th width=\"80\" {thr}>Snow</th></tr>"
            f"{''.join(rows)}</table>")
    return "".join(out)


def next_days(flag_days):
    lines = []
    for day in flag_days[1:]:
        locs = list(day["locations"].values())
        highs = [v["summary"]["high"] for v in locs if v["summary"]["high"] is not None]
        lows = [v["summary"]["low"] for v in locs if v["summary"]["low"] is not None]
        conds = {}
        for v in locs:
            conds.setdefault(v["summary"]["conditions"], []).append(v["name"])
        if len(conds) == 1:
            c = f"{next(iter(conds))} across the network."
        else:
            main = max(conds, key=lambda c: len(conds[c]))
            others = "; ".join(f"{c.lower()} at {', '.join(n)}" for c, n in conds.items() if c != main)
            c = f"Mostly {main.lower()}; {others}."
        temps = f" Highs {min(highs)}° to {max(highs)}°, lows {min(lows)}° to {max(lows)}°." if highs and lows else ""
        nflag = sum(1 for v in locs if v["status"] == "flag") + sum(1 for r in day["runs"] if r["status"] == "flag")
        tag = f" <span style=\"color:{FLAG_BAR};font-weight:bold\">{nflag} flag{'s' if nflag != 1 else ''}, listed above.</span>" if nflag else ""
        lines.append(f"<div style=\"{FONT}font-size:14px;line-height:1.6;color:{INK}\"><b>{e(day['label'])}:</b> {e(c)}{temps}{tag}</div>")
    return "".join(lines)


def outlook(outlook_days):
    if not outlook_days:
        return ""
    rows = []
    for o in outlook_days:
        color, word = OUTLOOK[o["level"]]
        if o["locations"] and all(v.get("no_data") for v in o["locations"].values()):
            color, word = "#e1e6df", "Not available yet"
        td = f"style=\"{FONT}font-size:14px;color:{INK};padding:7px 8px 7px 0;border-bottom:1px solid {LINE};vertical-align:top\""
        rows.append(
            f"<tr><td width=\"110\" {td}><b>{e(o['label'])}</b><br><span style=\"font-size:12px;color:{MUTED}\">{e(o['confidence'])} confidence</span></td>"
            f"<td width=\"135\" {td}><span style=\"display:inline-block;background:{color};padding:2px 8px;font-size:12px;font-weight:bold;white-space:nowrap\">{word}</span></td>"
            f"<td {td}>{e(o['summary'])}</td></tr>")
    return (f"<div style=\"{FONT}font-size:15px;font-weight:bold;color:{INK};margin:22px 0 2px\">What's coming, days 4 to 7</div>"
            f"<div style=\"{FONT}font-size:12px;color:{MUTED};margin-bottom:6px\">Early signals only. Flags and truck checks start once a day is within 3 days.</div>"
            f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;table-layout:fixed\">"
            f"<colgroup><col width=\"110\"><col width=\"135\"><col></colgroup>{''.join(rows)}</table>")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    r = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    flag_days, outlook_days = r["flag_days"], r["outlook_days"]
    today = flag_days[0]
    gen = datetime.fromisoformat(r["generated"])

    # RUN_MODE is set by the workflow: morning, noon or manual
    mode = os.environ.get("RUN_MODE", "morning")
    tag = {"morning": "MWS 6 AM", "noon": "MWS noon update", "manual": "MWS update"}.get(mode, "MWS")
    heading = {"morning": "6 AM network forecast", "noon": "Noon update, sent because there are flags,",
               "manual": "On-demand update"}.get(mode, "Network forecast")

    flags, watches = collect_items(flag_days)
    if flags:
        subject = f"⚠️ {tag}: {len(flags)} weather flag{'s' if len(flags) != 1 else ''} in the next 3 days"
    elif watches:
        subject = f"✅ {tag}: no flags, {len(watches)} heads-up{'s' if len(watches) != 1 else ''}"
    else:
        subject = f"✅ {tag}: all clear, no weather flags"
    if any(o["level"] == "a" for o in outlook_days):
        subject += " (potential hazard later this week)"

    page_url = cfg.get("links", {}).get("weather_page")
    button = (f"<div style=\"margin:22px 0 0\"><a href=\"{e(page_url)}\" style=\"display:inline-block;background:{GREEN};color:#ffffff;"
              f"text-decoration:none;{FONT}font-size:14px;font-weight:bold;padding:10px 18px\">View the weather map</a></div>") if page_url else ""

    notes = []
    if r.get("missing"):
        notes.append(f"No forecast available for: {', '.join(r['missing'])}.")
    if "open_meteo" in r.get("sources_used", []):
        notes.append("Some locations used the backup forecast source because the Weather Service was unavailable.")
    note_html = "".join(f"<div style=\"{FONT}font-size:13px;color:{FLAG_TXT};margin-top:8px\">{e(n)}</div>" for n in notes)

    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(subject)}</title></head>
<body style="margin:0;padding:0;background:#ffffff">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:16px 10px">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%"><tr><td>
<div style="{FONT}font-size:22px;font-weight:bold;color:{GREEN}">Mosaic Weather System</div>
<div style="{FONT}font-size:13px;color:{MUTED};margin-bottom:12px">{heading} for {gen.strftime('%A, %b ')}{gen.day}. The weather map is attached.</div>
{note_html}
{banner(flags, watches)}
<div style="{FONT}font-size:15px;font-weight:bold;color:{INK};margin:20px 0 0">Today, {e(today['label'])}</div>
{forecast_tables(today, cfg)}
<div style="{FONT}font-size:15px;font-weight:bold;color:{INK};margin:20px 0 6px">Next 2 days</div>
{next_days(flag_days)}
{outlook(outlook_days)}
{button}
<div style="{FONT}font-size:12px;color:#7a867d;margin-top:18px;border-top:1px solid {LINE};padding-top:10px">
Sources: National Weather Service, with Open-Meteo as backup. Generated {gen.strftime('%I:%M %p').lstrip('0')}.
Reporting only, not a go or no-go call.</div>
</td></tr></table>
</td></tr></table>
</body></html>"""

    HTML_PATH.write_text(body, encoding="utf-8")
    JSON_PATH.write_text(json.dumps({"subject": subject, "html": body}, ensure_ascii=False), encoding="utf-8")
    print(f"Subject: {subject}")
    print(f"Saved {HTML_PATH.relative_to(ROOT)} and {JSON_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
