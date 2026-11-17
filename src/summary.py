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
    sev = "Significant " if e["severity"] == "significant" else ""
    title = lower_first(e["title"]) if sev else e["title"]
    return f"{sev}{title} for {e['regions']}, {e['when']}."


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
        for e in flags[:2]:
            parts.append(event_sentence(e))
        if len(flags) > 2:
            parts.append(f"Plus {len(flags) - 2} more event{'s' if len(flags) > 3 else ''}, listed below.")
        if runs:
            names = []
            for r in runs:
                n = f"{r['label'].split(',')[0]} {r['name']}"
                if n not in names:
                    names.append(n)
            parts.append("Truck runs affected: " + ", ".join(names) + ".")
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

Rewrite the draft into 2 or 3 short, plain sentences a busy manager can read in 10 seconds. Lead with \
the most serious item and say where and when. Mention affected truck runs by name. Use only the facts \
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
