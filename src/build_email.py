"""
Mosaic Weather Advisory and Alert System: email builder

Reads output/results.json and config/network.yaml and writes:
  output/email.html   the email body (open it in a browser to preview)
  output/email.json   subject + body together, for Power Automate to send

Two layouts:
  Flag days   bottom line, weather events, affected truck runs, then the full
              forecast tables
  Calm days   a short version: bottom line, a few lines of forecast, outlook

Built with tables and inline styles, since that's what Outlook renders reliably.

Run it with:  python src/build_email.py
"""

import json
import os
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "network.yaml"
RESULTS_PATH = ROOT / "output" / "results.json"
HTML_PATH = ROOT / "output" / "email.html"
JSON_PATH = ROOT / "output" / "email.json"

GREEN = "#4d8723"
INK = "#1c2620"
MUTED = "#5b6a60"
LINE = "#e1e6df"
ZEBRA = "#f3f6f1"
FLAG_BG, FLAG_BAR, FLAG_TXT = "#fbe5e1", "#c23b2a", "#9b2c1e"
WATCH_BG, WATCH_BAR = "#fbf0d8", "#b77a0e"
CLEAR_BG = "#e7f1dc"
SEV_BADGE = {"significant": ("#c23b2a", "#ffffff", "Significant"),
             "minor": ("#f3c4bb", "#7a2318", "Minor"),
             None: ("#f4e07a", "#1c2620", "Heads-up")}
CHANGE_BADGE = {"new": ("#1c2620", "NEW"), "worse": ("#c23b2a", "WORSE"), "better": ("#4d8723", "BETTER")}
OUTLOOK = {"a": ("#f6b46a", "Potential hazard"), "y": ("#f4e07a", "Worth watching"), "g": ("#c6e5a6", "Nothing notable")}
STATE_NAMES = {"VA": "Virginia", "MD": "Maryland", "NC": "North Carolina", "DC": "Washington, DC", "DE": "Delaware", "WV": "West Virginia"}
FONT = "font-family:Arial,Helvetica,sans-serif;"
TABLE = "role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;table-layout:fixed\""


def e(s):
    return escape(str(s)) if s is not None else ""


def temp(v):
    return "–" if v is None else f"{v}°"


def clock_str(hm):
    t = datetime.strptime(hm, "%H:%M")
    return t.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def day_part(dt):
    """'Mon night', 'Tue AM', 'Tue PM'. Just after midnight still counts as the night before."""
    if dt.hour < 4:
        return f"{(dt - timedelta(hours=5)).strftime('%a')} night"
    return f"{dt.strftime('%a')} {'AM' if dt.hour < 12 else 'PM' if dt.hour < 18 else 'night'}"


def short_when(start_iso, end_iso):
    """'Mon night to Tue AM', for subject lines."""
    a = day_part(datetime.fromisoformat(start_iso))
    b = day_part(datetime.fromisoformat(end_iso) - timedelta(minutes=1))
    return a if a == b else f"{a} to {b}"


def badge(bg, fg, text):
    return (f"<span style=\"display:inline-block;background:{bg};color:{fg};{FONT}font-size:11px;font-weight:bold;"
            f"padding:2px 7px;white-space:nowrap\">{e(text)}</span>")


def change_badge(c):
    if c not in CHANGE_BADGE:
        return ""
    bg, text = CHANGE_BADGE[c]
    return " " + badge(bg, "#ffffff", text)


# ----------------------------------------------------------------------
# Sections
# ----------------------------------------------------------------------
def bottom_line_box(r, worst):
    bar, bg = {"flag": (FLAG_BAR, FLAG_BG), "watch": (WATCH_BAR, WATCH_BG)}.get(worst, (GREEN, CLEAR_BG))
    icon = {"flag": "&#9888;&#65039;", "watch": "&#128993;"}.get(worst, "&#9989;")
    note = (f"<div style=\"{FONT}font-size:11px;color:{MUTED};margin-top:6px\">Summary written by Claude from the forecast data below.</div>"
            if r.get("bottom_line_by_ai") else "")
    return (f"<table {TABLE}><tr><td width=\"5\" style=\"background:{bar}\"></td>"
            f"<td style=\"background:{bg};padding:12px 14px\">"
            f"<div style=\"{FONT}font-size:12px;font-weight:bold;letter-spacing:.06em;color:{MUTED};text-transform:uppercase\">{icon} The bottom line</div>"
            f"<div style=\"{FONT}font-size:16px;line-height:1.45;color:{INK};margin-top:4px\">{e(r['bottom_line'])}</div>{note}"
            f"</td></tr></table>")


def events_table(events):
    if not events:
        return ""
    td = f"style=\"{FONT}font-size:14px;color:{INK};padding:8px 8px 8px 0;border-bottom:1px solid {LINE};vertical-align:top\""
    rows = []
    for ev in events:
        bg, fg, word = SEV_BADGE[ev["severity"] if ev["level"] == "flag" else None]
        where = ev["regions"] if ev["regions"] in ("the whole network", "most of the network") else ev["regions"]
        alerts = f" {', '.join(ev['alerts'])} in effect." if ev.get("alerts") else ""
        rows.append(
            f"<tr><td width=\"100\" {td}>{badge(bg, fg, word)}</td>"
            f"<td {td}><b>{e(ev['title'])}: {e(where)}</b>{change_badge(ev.get('change'))}<br>"
            f"<span style=\"font-size:13px;color:{MUTED}\">{e(', '.join(ev['names']))}. {e(ev['detail'])}{e(alerts)}</span></td>"
            f"<td width=\"160\" {td}>{e(ev['when'])}</td></tr>")
    return (f"<div style=\"{FONT}font-size:15px;font-weight:bold;color:{INK};margin:20px 0 6px\">Weather events, next 3 days</div>"
            f"<table {TABLE}><colgroup><col width=\"100\"><col><col width=\"160\"></colgroup>{''.join(rows)}</table>")


def runs_table(run_events):
    if not run_events:
        return ""
    td = f"style=\"{FONT}font-size:14px;color:{INK};padding:8px 8px 8px 0;border-bottom:1px solid {LINE};vertical-align:top\""
    rows = []
    for r in run_events:
        bg, fg, word = SEV_BADGE[r["severity"] if r["level"] == "flag" else None]
        rows.append(
            f"<tr><td width=\"100\" {td}>{badge(bg, fg, word)}</td>"
            f"<td {td}><b>{e(r['label'].split(',')[0])} {e(r['name'])}</b>{change_badge(r.get('change'))}"
            f" <span style=\"color:{MUTED}\">leaves {e(clock_str(r['depart']))}</span><br>"
            f"<span style=\"font-size:13px;color:{MUTED}\">{e(r['where'])}, about {e(r['from'])} to {e(r['to'])}: {e(', '.join(r['why']))}.</span></td></tr>")
    return (f"<div style=\"{FONT}font-size:15px;font-weight:bold;color:{INK};margin:20px 0 6px\">Truck runs</div>"
            f"<table {TABLE}><colgroup><col width=\"100\"><col></colgroup>{''.join(rows)}</table>")


def cleared_line(cleared):
    if not cleared:
        return ""
    items = "; ".join(f"{c['title']}{' for ' + c['regions'] if c['regions'] else ''}, {c['when']}" for c in cleared)
    return (f"<div style=\"{FONT}font-size:13px;color:{GREEN};margin-top:10px\"><b>No longer expected since the last report:</b> "
            f"<span style=\"color:{INK}\">{e(items)}</span></div>")


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


def day_line(day):
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
    return f"<div style=\"{FONT}font-size:14px;line-height:1.6;color:{INK}\"><b>{e(day['label'])}:</b> {e(c)}{temps}</div>"


def outlook(outlook_days):
    if not outlook_days:
        return ""
    if all(o["level"] == "g" for o in outlook_days) and not any(
            o["locations"] and all(v.get("no_data") for v in o["locations"].values()) for o in outlook_days):
        highs = [v["high"] for o in outlook_days for v in o["locations"].values() if v.get("high") is not None]
        rng = f" Highs {min(highs)}° to {max(highs)}°." if highs else ""
        return (f"<div style=\"{FONT}font-size:15px;font-weight:bold;color:{INK};margin:20px 0 6px\">What's coming, days 4 to 7</div>"
                f"<div style=\"{FONT}font-size:14px;line-height:1.6;color:{INK}\"><b>{e(outlook_days[0]['label'])} to {e(outlook_days[-1]['label'])}:</b> "
                f"nothing notable.{rng} <span style=\"color:{MUTED}\">(Early signals only.)</span></div>")
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
            f"<table {TABLE}><colgroup><col width=\"110\"><col width=\"135\"><col></colgroup>{''.join(rows)}</table>")


def how_to_read():
    return (f"<div style=\"{FONT}font-size:12px;line-height:1.5;color:{MUTED};margin-top:18px;border-top:1px solid {LINE};padding-top:10px\">"
            f"<b style=\"color:{INK}\">How to read this:</b> "
            f"<b>Significant</b> means heavy snow (3 inches or more in 12 hours), a tenth of an inch or more of ice, or an official winter storm or ice storm warning. "
            f"<b>Minor</b> means it meets a flag (any ice or sleet, rush-hour snow, an advisory) but at lower amounts. "
            f"<b>Heads-up</b> means close to a flag, or a risk like refreeze or gusty wind. "
            f"<b>NEW</b>, <b>WORSE</b> and <b>BETTER</b> compare with the last report sent. "
            f"Truck runs are checked for the hours each truck is expected on each stretch. "
            f"Open the attached map for routes, times and traffic cameras.</div>")


# ----------------------------------------------------------------------
# Subject line
# ----------------------------------------------------------------------
def subject_for(r, tag):
    flags = [x for x in r["events"] if x["level"] == "flag"]
    runs = [x for x in r["run_events"] if x["level"] == "flag"]
    watches = [x for x in r["events"] if x["level"] != "flag"] + [x for x in r["run_events"] if x["level"] != "flag"]
    changed = any(x.get("change") in ("new", "worse") for x in flags + runs)
    if flags:
        top = flags[0]
        s = f"⚠️ {tag}: {top['title']}, {top['regions']}, {short_when(top['start'], top['end'])}"
        more = len(flags) - 1
        if more:
            s += f" + {more} more"
        if changed:
            s += " (updated)"
        return s
    if runs:
        top = runs[0]
        return f"⚠️ {tag}: {top['name']} run flagged, {top['label'].split(',')[0]}" + (" (updated)" if changed else "")
    if watches:
        w = watches[0]
        what = f"{w['title']}, {w['regions']}" if "title" in w else f"{w['name']} run"
        return f"🟡 {tag}: no flags, heads-up: {what}"
    return f"✅ {tag}: all clear for the next 3 days"


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    r = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    flag_days, outlook_days = r["flag_days"], r["outlook_days"]
    gen = datetime.fromisoformat(r["generated"])

    mode = os.environ.get("RUN_MODE", "morning")
    tag = {"morning": "MWS 6 AM", "noon": "MWS noon update", "manual": "MWS update"}.get(mode, "MWS")
    heading = {"morning": "6 AM network forecast", "noon": "Noon update, sent because there are flags,",
               "manual": "On-demand update"}.get(mode, "Network forecast")

    flags = [x for x in r["events"] if x["level"] == "flag"]
    run_flags = [x for x in r["run_events"] if x["level"] == "flag"]
    worst = "flag" if flags or run_flags else "watch" if r["events"] or r["run_events"] else "clear"
    subject = subject_for(r, tag)
    if any(o["level"] == "a" for o in outlook_days):
        subject += " (watch later this week)"

    notes = []
    if r.get("missing"):
        notes.append(f"No forecast available for: {', '.join(r['missing'])}.")
    if "open_meteo" in r.get("sources_used", []):
        notes.append("Some locations used the backup forecast source because the Weather Service was unavailable.")
    note_html = "".join(f"<div style=\"{FONT}font-size:13px;color:{FLAG_TXT};margin:0 0 8px\">{e(n)}</div>" for n in notes)

    if worst == "flag":
        body_main = (events_table(r["events"]) + runs_table(r["run_events"]) + cleared_line(r.get("cleared"))
                     + f"<div style=\"{FONT}font-size:15px;font-weight:bold;color:{INK};margin:22px 0 0\">Today, {e(flag_days[0]['label'])}</div>"
                     + forecast_tables(flag_days[0], cfg)
                     + f"<div style=\"{FONT}font-size:15px;font-weight:bold;color:{INK};margin:20px 0 6px\">Next 2 days</div>"
                     + "".join(day_line(d) for d in flag_days[1:]))
    else:
        # Calm-day short version: heads-ups (if any) and a few lines of forecast
        body_main = (events_table(r["events"]) + runs_table(r["run_events"]) + cleared_line(r.get("cleared"))
                     + f"<div style=\"{FONT}font-size:15px;font-weight:bold;color:{INK};margin:20px 0 6px\">Next 3 days</div>"
                     + "".join(day_line(d) for d in flag_days))

    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(subject)}</title></head>
<body style="margin:0;padding:0;background:#ffffff">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:16px 10px">
<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%"><tr><td>
<div style="{FONT}font-size:22px;font-weight:bold;color:{GREEN}">Mosaic Weather System</div>
<div style="{FONT}font-size:13px;color:{MUTED};margin-bottom:12px">{heading} for {gen.strftime('%A, %b ')}{gen.day}. The weather map is attached.</div>
{note_html}
{bottom_line_box(r, worst)}
{body_main}
{outlook(outlook_days)}
{how_to_read()}
<div style="{FONT}font-size:12px;color:#7a867d;margin-top:10px">
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
