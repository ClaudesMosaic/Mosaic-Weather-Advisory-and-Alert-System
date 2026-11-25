"""
Mosaic Weather Advisory and Alert System: the bottom line

Writes the two or three sentence summary at the top of the email and map.

  1. A rule-based summary is always built from the events. It is free and
     never says anything that isn't in the data.
  2. If an ANTHROPIC_API_KEY secret is set, Claude (Haiku) rewrites that
     summary into smoother plain English, using only the facts provided.
     If the call fails or the answer looks wrong, the rule-based summary
     is used instead, so the email never depends on it.

Used by build_email.py and build_map.py; not run on its own.
"""

import json
import os
import re

import requests

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("MWS_SUMMARY_MODEL", "claude-haiku-5-5")


def lower_first(s):
    return s[:1].lower() + s[1:] if s else s


def event_sentence(e):
    if e.get("hazard") == "alert":
        return f"{e['title']} for {e['regions']}, {e['when']}."  # an official alert name says its own severity
    sev = "Significant " if e["severity"] == "significant" else ""
    title = lower_first(e["title"]) if sev else e["title"]
    return f"{sev}{title} for {e['regions']}, {e['when']}."


def short_why(why):
    """'flood watch in effect', 'gusts to 40 mph' -> 'flood watch, gusts to 40 mph'"""
    out = []
    for w in why:
        w = re.sub(r" (in effect|expected)$", "", w.strip().rstrip("."))
        w = re.sub(r", near the .*$", "", w)
        if w and w not in out:
            out.append(w)
    return ", ".join(out[:3]) or "flagged"


def mark_severity(results):
    """Tag each flagged location, run and run stretch in flag_days with "sev": minor or significant,
    taken from the events and truck runs that cover them. Used to color Minor differently from Significant."""
    order = {None: 0, "minor": 1, "significant": 2}
    runs = {(x["date"], x["id"]): x for x in results.get("run_events", [])}
    for i, day in enumerate(results.get("flag_days", [])):
        for k, v in day["locations"].items():
            if v.get("status") != "flag":
                continue
            sevs = [e["severity"] for e in results.get("events", [])
                    if e["level"] == "flag" and i in e.get("days", []) and k in e["locations"]]
            v["sev"] = max(sevs, key=order.get) if sevs else "significant"
        for run in day.get("runs", []):
            if run.get("status") != "flag":
                continue
            re_ = runs.get((day["date"], run["id"]))
            run["sev"] = (re_ or {}).get("severity") or "significant"
            for p in run["points"]:
                if p["status"] == "flag":
                    p["sev"] = run["sev"]
    return results


def lines(text):
    """Split the bottom line into one item per line for display."""
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text or "") if x.strip()]


def rule_based(results):
    flags = [e for e in results["events"] if e["level"] == "flag"]
    watches = [e for e in results["events"] if e["level"] != "flag"]
    runs = [r for r in results["run_events"] if r["level"] == "flag"]
    parts = []
    if not flags and not runs:
        if watches:
            parts.append("No weather flags in the next 3 days.")
            parts.append("Worth knowing: " + "; ".join(f"{lower_first(e['title'])} for {e['regions']}, {e['when']}" for e in watches[:2]) + ".")
        else:
            parts.append("No weather concerns for any location or truck run in the next 3 days.")
    else:
        for e in flags[:3]:
            parts.append(event_sentence(e))
        if len(flags) > 3:
            parts.append(f"Plus {len(flags) - 3} more event{'s' if len(flags) > 4 else ''}, listed below.")
        if runs:
            names = []
            for r in runs:
                n = f"{r['label'].split(',')[0]} {r['name']} ({short_why(r['why'])})"
                if n not in names:
                    names.append(n)
            parts.append("Truck runs affected: " + "; ".join(names) + ".")
    if results.get("cleared"):
        parts.append("No longer expected: " + "; ".join(f"{c['title']}{' for ' + c['regions'] if c['regions'] else ''}, {c['when']}" for c in results["cleared"][:3]) + ".")
    return " ".join(parts)


def facts_for_ai(results):
    keep = ("title", "level", "severity", "regions", "names", "when", "detail", "alerts", "change")
    return {
        "events": [{k: e.get(k) for k in keep} for e in results["events"]],
        "truck_runs": [{k: r.get(k) for k in ("label", "name", "level", "severity", "where", "from", "to", "why", "change")}
                       for r in results["run_events"]],
        "no_longer_expected": results.get("cleared", []),
    }


PROMPT = """You write the opening summary of a daily weather report for a tile distributor's \
logistics manager. It covers 9 stores, a distribution center (the CSC in Doswell, VA) and overnight \
truck runs in Virginia, Maryland and North Carolina.

Rewrite the draft as short, plain sentences a busy manager can read in 10 seconds, one sentence per \
weather event and one for the truck runs; each sentence will be shown on its own line. Lead with the \
most serious item and say what it is (for example high wind, ice, heavy snow or a flood watch), where and \
when. Mention affected truck runs by name with what flagged them. Use only the facts \
given; never add numbers, places, times or advice that are not in them. This is reporting only, so do \
not recommend closing, delaying or cancelling anything. Do not use em dashes. Reply with the summary \
text only.

Facts (JSON):
{facts}

Draft:
{draft}"""


AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:\"|in\b|inch|inches|°|degrees|mph)", re.I)
TIME = re.compile(r"\b(\d{1,2})(?::(\d\d))?\s*([AP])\.?M\b", re.I)
WORD_AMOUNT = re.compile(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+(inch|inches|degrees)\b", re.I)


def grounded(text, facts_text):
    """Every amount and clock time in the AI text must appear in the facts."""
    if WORD_AMOUNT.search(text):
        return False
    known = {float(m) for m in AMOUNT.findall(facts_text)}
    if any(float(m) not in known for m in AMOUNT.findall(text)):
        return False
    times = {(int(h), int(m or 0), ap.upper()) for h, m, ap in TIME.findall(facts_text)}
    times |= {(12, 0, "A"), (12, 0, "P")}  # midnight and noon appear as words in the facts
    return all((int(h), int(m or 0), ap.upper()) in times for h, m, ap in TIME.findall(text))


def ai_rewrite(results, draft):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = requests.post(API_URL, timeout=30, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 300,
                  "messages": [{"role": "user", "content": PROMPT.format(
                      facts=json.dumps(facts_for_ai(results)), draft=draft)}]})
        if r.status_code != 200:
            print(f"AI summary skipped (HTTP {r.status_code}); using the rule-based summary.")
            return None
        text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text").strip()
    except (requests.RequestException, ValueError) as e:
        print(f"AI summary skipped ({e}); using the rule-based summary.")
        return None
    text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text).strip()
    facts_text = json.dumps(facts_for_ai(results), ensure_ascii=False) + " " + draft
    if not (20 <= len(text) <= 700) or not grounded(text, facts_text):
        print("AI summary didn't pass checks; using the rule-based summary.")
        return None
    return text


def bottom_line(results):
    """Returns (summary text, written_by_ai)."""
    draft = rule_based(results)
    ai = ai_rewrite(results, draft) if (results["events"] or results["run_events"]) else None
    return (ai or draft), bool(ai)
