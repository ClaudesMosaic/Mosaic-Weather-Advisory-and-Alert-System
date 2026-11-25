"""
Mosaic Weather Advisory and Alert System: what changed since the last email

Compares this run with the last report that was actually emailed (saved in
state/last_results.json). Comparing with the last email, not the last check,
means small changes add up until they matter, and a flag that flickers off
and back on doesn't send two emails.

Each event and truck run gets a badge (new, worse, better). On top of that,
results["changes"] lists the changes that are worth an email on their own:

  new       a new flag
  worse     a flag got more serious (heads-up to flagged, minor to significant)
  better    a flag eased (significant to minor, flagged to heads-up)
  dropped   a flag is no longer expected
  amount    snow total moved 1" or more, or ice moved 0.10" or more
  timing    a flagged event moved into or out of a rush hour
  alert     an official alert was issued or ended

Heads-up-only changes get badges but never trigger an email by themselves.
Improvements are only reported for events that haven't started yet, since a
storm in progress naturally shows less remaining snow with each check.

Used by evaluate.py; not run on its own.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import events as ev_mod

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "last_results.json"
RANK = {"clear": 0, "watch": 1, "flag": 2}
SEV = {None: 0, "minor": 1, "significant": 2}
MAX_AGE = timedelta(hours=36)

SNOW_STEP = 1.0     # inches of snow that count as a change
ICE_STEP = 0.10     # inches of ice that count as a change


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


def _level_word(x):
    if x["level"] == "flag":
        return x.get("severity") or "flagged"
    return "heads-up" if x["level"] == "watch" else "clear"


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


def _name(e):
    return f"{e['title']}, {e['regions']}"


def _day(label):
    return label.split(",")[0]


def _add(out, kind, rank, sev, phrase, when):
    out.append({"kind": kind, "rank": rank, "severity": sev, "phrase": phrase, "when": when})


def compare(results, now):
    prev = load_previous(now)
    results["compared_to"] = prev["generated"] if prev else None
    results["cleared"] = []
    results["changes"] = []
    if not prev:
        for x in results["events"] + results["run_events"]:
            x["change"] = None
        return

    out = []
    olds = list(prev.get("events", []))
    pairs, used = _pair(results["events"], olds)

    for i, e in enumerate(results["events"]):
        started = datetime.fromisoformat(e["start"]) < now - timedelta(hours=1)
        flagged = e["level"] == "flag"
        if i not in pairs:
            e["change"] = "new"
            if flagged:
                _add(out, "new", 3, e["severity"], f"New: {_name(e)}", e["when"])
            continue
        o = olds[pairs[i]]
        either = flagged or o["level"] == "flag"
        e["change"] = None
        if _score(e) > _score(o):
            e["change"] = "worse"
            if either:
                _add(out, "worse", 3, e["severity"], f"{_name(e)} now {_level_word(e)}", e["when"])
        elif _score(e) < _score(o) and not started:
            e["change"] = "better"
            if either:
                _add(out, "better", 1, o.get("severity"), f"{_name(e)} eased to {_level_word(e)}", e["when"])
        if not either:
            continue

        # Amounts
        ds = e.get("snow_in", 0) - o.get("snow_in", 0)
        if abs(ds) >= SNOW_STEP and (ds > 0 or not started):
            if e.get("snow_in", 0) < 0.1:
                _add(out, "amount", 1.5, o.get("severity"),
                     f"{e['regions']} snow no longer expected (was {o.get('snow_in', 0):.1f}\")", e["when"])
            else:
                word = "up" if ds > 0 else "down"
                _add(out, "amount", 3 if ds > 0 else 1, e["severity"],
                     f"{e['regions']} snow {word} to {e['snow_in']:.1f}\" (was {o.get('snow_in', 0):.1f}\")", e["when"])
            if ds > 0 and e["change"] is None:
                e["change"] = "worse"
        di = e.get("ice_in", 0) - o.get("ice_in", 0)
        if abs(di) >= ICE_STEP and (di > 0 or not started):
            if e.get("ice_in", 0) < 0.01:
                _add(out, "amount", 1.5, o.get("severity"),
                     f"{e['regions']} ice no longer expected (was {o.get('ice_in', 0):.2f}\")", e["when"])
            else:
                word = "up" if di > 0 else "down"
                _add(out, "amount", 3 if di > 0 else 1, e["severity"],
                     f"{e['regions']} ice {word} to {e['ice_in']:.2f}\" (was {o.get('ice_in', 0):.2f}\")", e["when"])
            if di > 0 and e["change"] is None:
                e["change"] = "worse"

        # Timing: rush hours entered or left (ignoring rush hours already over)
        live = lambda rs: {r["label"] for r in rs if datetime.fromisoformat(r["end"]) > now}  # noqa: E731
        if "rush" not in o:
            continue  # last email came from an older version without rush-hour tracking
        now_r, was_r = live(e.get("rush", [])), live(o.get("rush", []))
        for lab in sorted(now_r - was_r):
            _add(out, "timing", 2, e["severity"], f"{_name(e)} now during {lab}", e["when"])
        if not started:
            for lab in sorted(was_r - now_r):
                _add(out, "timing", 1, e["severity"], f"{_name(e)} no longer during {lab}", e["when"])


    # Official alerts, tracked by location
    def by_pair(alerts):
        return {(a["name"], a["loc"]): a for a in alerts}
    cur, was = by_pair(results.get("alerts", [])), by_pair(prev.get("alerts", []))
    if "alerts" not in prev:
        cur = was = {}  # last email came from an older version without alert tracking
    for kind, pairs_ in (("issued", [cur[p] for p in cur.keys() - was.keys()]),
                         ("ended", [was[p] for p in was.keys() - cur.keys() if datetime.fromisoformat(was[p]["end"]) > now])):
        for name in sorted({a["name"] for a in pairs_}):
            grp = [a for a in pairs_ if a["name"] == name]
            regions = "/".join(dict.fromkeys(a["region"] for a in grp))
            start = min(datetime.fromisoformat(a["start"]) for a in grp)
            end = max(datetime.fromisoformat(a["end"]) for a in grp)
            when = ev_mod.when_label(start, end)
            if kind == "issued":
                _add(out, "alert", 3, "significant" if "Warning" in name else "minor", f"{name} issued, {regions}", when)
            else:
                _add(out, "alert", 1, None, f"{name} ended early, {regions}", when)

    for j, o in enumerate(olds):
        if j in used or o["level"] != "flag":
            continue
        if datetime.fromisoformat(o["end"]) <= now or datetime.fromisoformat(o["start"]) < now:
            continue  # already over or under way; not a forecast change
        results["cleared"].append({"title": o["title"], "regions": o["regions"], "when": o["when"]})
        _add(out, "dropped", 1, o.get("severity"), f"{_name(o)} dropped", o["when"])

    # Truck runs
    prev_runs = {(r["date"], r["id"]): r for r in prev.get("run_events", [])}
    seen = set()
    for r in results["run_events"]:
        key = (r["date"], r["id"])
        seen.add(key)
        o = prev_runs.get(key)
        departed = datetime.fromisoformat(f"{r['date']}T{r['depart']}").replace(tzinfo=now.tzinfo) < now
        flagged = r["level"] == "flag"
        r["change"] = None
        if o is None:
            r["change"] = "new"
            if flagged:
                _add(out, "new", 3, r["severity"], f"{r['name']} ({_day(r['label'])}) now flagged", r["label"])
        elif _score(r) > _score(o):
            r["change"] = "worse"
            if flagged or o["level"] == "flag":
                _add(out, "worse", 3, r["severity"], f"{r['name']} ({_day(r['label'])}) now {_level_word(r)}", r["label"])
        elif _score(r) < _score(o) and not departed:
            r["change"] = "better"
            if o["level"] == "flag":
                _add(out, "better", 1, o.get("severity"), f"{r['name']} ({_day(r['label'])}) eased to {_level_word(r)}", r["label"])
    for key, o in prev_runs.items():
        if key in seen or o["level"] != "flag":
            continue
        dep = datetime.fromisoformat(f"{o['date']}T{o['depart']}").replace(tzinfo=now.tzinfo)
        if dep < now:
            continue
        results["cleared"].append({"title": f"{o['name']} run", "regions": "", "when": o["label"]})
        _add(out, "dropped", 1, o.get("severity"), f"{o['name']} ({_day(o['label'])}) flag dropped", o["label"])

    out.sort(key=lambda c: (-c["rank"], -SEV[c["severity"]]))
    results["changes"] = out
