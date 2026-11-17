"""
Mosaic Weather Advisory and Alert System: what changed since the last report

Compares this run's events and truck runs with the last report that was
actually sent (saved in state/last_results.json) and labels each one:
  new      not in the last report
  worse    higher level or severity, or noticeably more snow or ice
  better   lower level or severity
  cleared  in the last report, no longer expected

Used by evaluate.py; not run on its own.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "last_results.json"
RANK = {"clear": 0, "watch": 1, "flag": 2}
SEV = {None: 0, "minor": 1, "significant": 2}
MAX_AGE = timedelta(hours=36)


def load_previous(now):
    try:
        prev = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        gen = datetime.fromisoformat(prev["generated"])
        if now - gen > MAX_AGE or now <= gen:
            return None
        return prev
    except (OSError, ValueError, KeyError):
        return None


def _score(x):
    return RANK[x["level"]] * 10 + SEV[x.get("severity")]


def _near(e, o):
    s, en = datetime.fromisoformat(e["start"]), datetime.fromisoformat(e["end"])
    os_, oe = datetime.fromisoformat(o["start"]), datetime.fromisoformat(o["end"])
    return os_ <= en + timedelta(hours=6) and s <= oe + timedelta(hours=6) and set(o["locations"]) & set(e["locations"])


def _pair(events, olds):
    """Match same-type events first, then fall back to the same family (snow vs mix, etc.)."""
    pairs, used = {}, set()
    for strict in (True, False):
        for i, e in enumerate(events):
            if i in pairs:
                continue
            for j, o in enumerate(olds):
                if j in used or not _near(e, o):
                    continue
                same = o.get("hazard") == e.get("hazard") and (e.get("hazard") != "alert" or o.get("title") == e.get("title"))
                if same if strict else o.get("family") == e.get("family"):
                    pairs[i] = j
                    used.add(j)
                    break
    return pairs, used


def compare(results, now):
    prev = load_previous(now)
    results["compared_to"] = prev["generated"] if prev else None
    cleared = []
    if not prev:
        for e in results["events"]:
            e["change"] = None
        for r in results["run_events"]:
            r["change"] = None
        results["cleared"] = cleared
        return

    olds = list(prev.get("events", []))
    pairs, used = _pair(results["events"], olds)
    for i, e in enumerate(results["events"]):
        if i not in pairs:
            e["change"] = "new"
            continue
        o = olds[pairs[i]]
        if _score(e) > _score(o) or e.get("snow_in", 0) >= max(o.get("snow_in", 0) * 1.5, o.get("snow_in", 0) + 1) \
                or e.get("ice_in", 0) >= max(o.get("ice_in", 0) * 1.5, o.get("ice_in", 0) + 0.05):
            e["change"] = "worse"
        elif _score(e) < _score(o):
            e["change"] = "better"
        else:
            e["change"] = None
    for i, o in enumerate(olds):
        if i not in used and datetime.fromisoformat(o["end"]) > now and o["level"] == "flag":
            cleared.append({"title": o["title"], "regions": o["regions"], "when": o["when"]})

    prev_runs = {(r["date"], r["id"]): r for r in prev.get("run_events", [])}
    seen = set()
    for r in results["run_events"]:
        key = (r["date"], r["id"])
        seen.add(key)
        o = prev_runs.get(key)
        if o is None:
            r["change"] = "new"
        elif _score(r) > _score(o):
            r["change"] = "worse"
        elif _score(r) < _score(o):
            r["change"] = "better"
        else:
            r["change"] = None
    today = now.date().isoformat()
    for key, o in prev_runs.items():
        if key not in seen and key[0] >= today and o["level"] == "flag":
            cleared.append({"title": f"{o['name']} run", "regions": "", "when": o["label"]})
    results["cleared"] = cleared
