"""
Mosaic Weather Advisory and Alert System: weather events

Turns hour-by-hour hazards into events people recognize: one storm, one time
span, every location it touches, rated minor or significant. An overnight
event stays one event even when it crosses midnight.

Used by evaluate.py; not run on its own.
"""

from datetime import timedelta

RANK = {"clear": 0, "watch": 1, "flag": 2}
SEV_RANK = {None: 0, "minor": 1, "significant": 2}
GAP = timedelta(hours=2)        # hazards this close together count as one event

# Short region names for subject lines; a location can set its own with `region:`
DEFAULT_REGIONS = {
    "chantilly": "NOVA", "fairfax": "NOVA", "alexandria": "NOVA",
    "nbethesda": "MD", "owings": "MD",
    "csc": "Richmond area", "richmond": "Richmond area",
    "vabeach": "Hampton Roads", "raleigh": "NC", "greensboro": "NC",
}


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------

def is_flood(name):
    return "Flood" in (name or "")

def clock(dt):
    if dt.minute == 0 and dt.hour in (0, 12):
        return "midnight" if dt.hour == 0 else "noon"
    return dt.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def when_label(start, end):
    """'Mon 10 PM to Tue 9 AM', or 'Mon 8 PM to 11 PM' within one day."""
    a = f"{start.strftime('%a')} {clock(start)}"
    if end.date() == start.date() or (end - start <= timedelta(hours=24) and end.hour == 0 and end.minute == 0
                                      and (end - timedelta(minutes=1)).date() == start.date()):
        return f"{a} to {clock(end)}"
    return f"{a} to {end.strftime('%a')} {clock(end)}"


def region_of(cfg, key):
    loc = cfg["locations"].get(key, {})
    return loc.get("region") or DEFAULT_REGIONS.get(key) or loc.get("state", key)


REGION_ORDER = ["NOVA", "MD", "Richmond area", "Hampton Roads", "NC"]


def regions_text(cfg, keys):
    out = []
    for k in keys:
        r = region_of(cfg, k)
        if r not in out:
            out.append(r)
    out.sort(key=lambda r: REGION_ORDER.index(r) if r in REGION_ORDER else 99)
    total = {region_of(cfg, k) for k in cfg["locations"]}
    if len(out) == len(total):
        return "the whole network"
    if len(out) >= 4:
        return "most of the network"
    return "/".join(out)


def ice_types(hours):
    t = {x for h in hours for x in h["types"]}
    words = [w for k, w in (("freezing_rain", "freezing rain"), ("freezing_drizzle", "freezing drizzle"),
                            ("sleet", "sleet")) if k in t]
    return " and ".join(words) or "ice"


def runs_of(hours, keep, gap_hours=1):
    """Split hours into contiguous stretches where keep(h) is true."""
    out, cur = [], []
    for h in hours:
        if keep(h):
            if cur and h["dt"] - cur[-1]["dt"] > timedelta(hours=gap_hours + 1):
                out.append(cur)
                cur = []
            cur.append(h)
    if cur:
        out.append(cur)
    return out


# ----------------------------------------------------------------------
# Hazard spans for one point
# ----------------------------------------------------------------------
def point_spans(pt, start, end, cfg, x, refreeze_hours):
    """Every hazard stretch at one place between start and end."""
    rules = cfg["rules"]
    hours = pt.between(start, end)
    one_hr = timedelta(hours=1)
    spans = []

    for grp in runs_of(hours, lambda h: h["ice"]):
        amt = sum(h["ice_in"] for h in grp)
        spans.append({"hazard": "ice", "level": "flag",
                      "severity": "significant" if amt >= x["ice_sig"] else "minor",
                      "start": grp[0]["dt"], "end": grp[-1]["dt"] + one_hr,
                      "amount": round(amt, 2), "kinds": ice_types(grp)})

    heavy = rules["heavy_snow"]
    rush_min = rules["rush_hour_snow"]["min_inches"]
    frac = cfg.get("heads_up", {}).get("near_threshold_fraction", 0.75)
    for grp in runs_of(hours, lambda h: h["snow_in"] > 0, gap_hours=2):
        total = sum(h["snow_in"] for h in grp)
        peak = max(h["snow_in"] for h in grp)
        rush = 0.0
        for wname in rules["rush_hour_snow"].get("windows", ["am_rush", "pm_rush"]):
            w = cfg["windows"][wname]
            for h in grp:
                hm = h["dt"].strftime("%H:%M")
                if w["start"] <= hm < w["end"]:
                    rush += h["snow_in"]
        is_heavy = total >= heavy["min_inches_12h"] or peak >= heavy["min_inches_1h"]
        if is_heavy or rush >= rush_min:
            level = "flag"
        elif rush >= rush_min * frac:
            level = "watch"
        else:
            continue
        spans.append({"hazard": "snow", "level": level,
                      "severity": ("significant" if is_heavy else "minor") if level == "flag" else None,
                      "start": grp[0]["dt"], "end": grp[-1]["dt"] + one_hr,
                      "amount": round(total, 1), "heavy": is_heavy, "rush": rush >= rush_min})

    wanted = set(rules.get("official_alert", {}).get("events", []))
    for a in pt.alerts_between(start, end, wanted):
        on = max(a["onset_dt"] or start, start)
        off = min(a["ends_dt"] or end, end)
        spans.append({"hazard": "alert", "level": "flag", "name": a["event"],
                      "severity": "significant" if "Warning" in a["event"] else "minor",
                      "start": on, "end": off})

    for grp in runs_of(hours, lambda h: (h.get("gust_mph") or 0) >= x["gust_watch"]):
        top = max(h["gust_mph"] for h in grp)
        flag = top >= x["gust_flag"]
        spans.append({"hazard": "wind", "level": "flag" if flag else "watch",
                      "severity": "minor" if flag else None,
                      "start": grp[0]["dt"], "end": grp[-1]["dt"] + one_hr, "amount": top})

    am = cfg["windows"]["am_rush"]
    day = start
    while day < end:
        ws = day.replace(hour=int(am["start"][:2]), minute=int(am["start"][3:]))
        we = day.replace(hour=int(am["end"][:2]), minute=int(am["end"][3:]))
        cold = refreeze_hours(pt, pt.between(ws, we), cfg)
        if cold and not any(s["hazard"] == "ice" and s["start"] < we and s["end"] > ws for s in spans):
            spans.append({"hazard": "refreeze", "level": "watch", "severity": None,
                          "start": cold[0]["dt"], "end": cold[-1]["dt"] + one_hr,
                          "amount": min(h["temp_f"] for h in cold)})
        day += timedelta(days=1)
    return spans


def rush_windows(cfg, start, end):
    """The AM and PM rush hours an event overlaps, like 'Tue AM rush'."""
    out = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        for name, word in (("am_rush", "AM"), ("pm_rush", "PM")):
            w = cfg.get("windows", {}).get(name)
            if not w:
                continue
            ws = day.replace(hour=int(w["start"][:2]), minute=int(w["start"][3:]))
            we = day.replace(hour=int(w["end"][:2]), minute=int(w["end"][3:]))
            if ws < end and we > start:
                out.append({"label": f"{day.strftime('%a')} {word} rush", "end": we.isoformat(timespec="minutes")})
        day += timedelta(days=1)
    return out


def overlaps(a, b, gap=GAP):
    return a["start"] <= b["end"] + gap and b["start"] <= a["end"] + gap


# ----------------------------------------------------------------------
# Events across the network
# ----------------------------------------------------------------------
def build_events(spans_by_key, cfg):
    """Group location spans into network events."""
    names = {k: v["name"] for k, v in cfg["locations"].items()}
    order = list(cfg["locations"])
    groups = []
    for k in order:
        for s in spans_by_key.get(k, []):
            if s["hazard"] == "alert":
                continue
            # Same hazard, same time and same seriousness: heavy snow in one region
            # and light snow in another stay two separate events.
            grade = (s["level"], s.get("severity"))
            for g in groups:
                if g["hazard"] == s["hazard"] and g["grade"] == grade and overlaps(g, s):
                    g["start"], g["end"] = min(g["start"], s["start"]), max(g["end"], s["end"])
                    g["locs"].setdefault(k, []).append(s)
                    break
            else:
                groups.append({"hazard": s["hazard"], "grade": grade, "start": s["start"], "end": s["end"], "locs": {k: [s]}})

    # Snow then ice (or ice then snow) at the same places is one storm
    merged = []
    for g in sorted(groups, key=lambda g: g["start"]):
        prev = next((m for m in merged if {m["hazard"], g["hazard"]} <= {"snow", "ice", "mix"}
                     and set(m["locs"]) == set(g["locs"]) and overlaps(m, g)), None)
        if prev and prev["hazard"] != g["hazard"]:
            prev["sequence"] = prev.get("sequence", [prev["hazard"]]) + [g["hazard"]]
            prev["hazard"] = "mix"
            prev["end"] = max(prev["end"], g["end"])
            for k, ss in g["locs"].items():
                prev["locs"][k].extend(ss)
        else:
            merged.append(g)

    events = []
    for g in merged:
        spans = [s for ss in g["locs"].values() for s in ss]
        level = max((s["level"] for s in spans), key=RANK.get)
        sev = max((s["severity"] for s in spans), key=SEV_RANK.get) if level == "flag" else None
        keys = [k for k in order if k in g["locs"]]
        ev = {"hazard": g["hazard"], "level": level, "severity": sev,
              "start": g["start"], "end": g["end"], "locations": keys,
              "names": [names[k] for k in keys], "alerts": [],
              "snow_in": max((s["amount"] for s in spans if s["hazard"] == "snow"), default=0),
              "ice_in": max((s["amount"] for s in spans if s["hazard"] == "ice"), default=0)}
        ev.update(describe(g, spans))
        events.append(ev)

    # Official alerts: attach to the event they describe, otherwise list on their own
    for k in order:
        for s in spans_by_key.get(k, []):
            if s["hazard"] != "alert":
                continue
            # Winter alerts attach to the winter event they describe; flood alerts always stand alone
            host = None if is_flood(s["name"]) else next(
                (e for e in events if e["level"] == "flag" and e["hazard"] in ("snow", "ice", "mix")
                 and k in e["locations"] and overlaps(e, s, timedelta(0))), None)
            if host:
                if s["name"] not in host["alerts"]:
                    host["alerts"].append(s["name"])
                if s["severity"] == "significant":
                    host["severity"] = "significant"
                continue
            solo = next((e for e in events if e["hazard"] == "alert" and e["title"] == s["name"] and overlaps(e, s)), None)
            if solo:
                if k not in solo["locations"]:
                    solo["locations"].append(k)
                    solo["names"].append(names[k])
                solo["start"], solo["end"] = min(solo["start"], s["start"]), max(solo["end"], s["end"])
            else:
                events.append({"hazard": "alert", "level": "flag", "severity": s["severity"],
                               "start": s["start"], "end": s["end"], "locations": [k], "names": [names[k]],
                               "alerts": [], "title": s["name"], "detail": "Official alert from the National Weather Service."})

    for e in events:
        e["when"] = when_label(e["start"], e["end"])
        e["regions"] = regions_text(cfg, e["locations"])
        if e["hazard"] == "alert" and is_flood(e["title"]):
            e["family"] = "flood"
            e["detail"] = "Official flood alert from the National Weather Service. Watch for flooded roads and lots."
        else:
            e["family"] = "winter" if e["hazard"] in ("ice", "snow", "mix", "alert") else e["hazard"]
        e["rush"] = rush_windows(cfg, e["start"], e["end"])
    events.sort(key=lambda e: (-RANK[e["level"]], -SEV_RANK[e["severity"]], e["start"]))
    return events


def describe(g, spans):
    h = g["hazard"]
    if h == "ice":
        amt = max(s["amount"] for s in spans)
        kinds = sorted({s["kinds"] for s in spans}, key=len)[-1]
        extra = f", up to {amt:.2f}\" of ice" if amt >= 0.01 else ""
        return {"title": "Ice", "detail": f"{kinds.capitalize()}{extra}."}
    if h == "snow":
        amt = max(s["amount"] for s in spans)
        heavy = any(s.get("heavy") for s in spans)
        if any(s["level"] == "flag" for s in spans):
            title = "Heavy snow" if heavy else "Rush-hour snow"
        else:
            title = "Light snow"
        return {"title": title, "detail": f"Up to {amt:.1f}\" of snow."}
    if h == "mix":
        seq = g.get("sequence", [])
        words = {"snow": "snow", "ice": "ice"}
        title = " changing to ".join(words.get(s, s) for s in seq).capitalize() if seq else "Wintry mix"
        snow = max((s["amount"] for s in spans if s["hazard"] == "snow"), default=0)
        ice = max((s["amount"] for s in spans if s["hazard"] == "ice"), default=0)
        kinds = next((s["kinds"] for s in spans if s["hazard"] == "ice"), "ice")
        parts = []
        if snow:
            parts.append(f"up to {snow:.1f}\" of snow")
        if ice >= 0.01:
            parts.append(f"up to {ice:.2f}\" of ice from {kinds}")
        return {"title": title, "detail": (" then ".join(parts) or "Snow and ice").capitalize() + "."}
    if h == "wind":
        top = max(s["amount"] for s in spans)
        flag = any(s["level"] == "flag" for s in spans)
        return {"title": "High wind" if flag else "Gusty winds",
                "detail": f"Gusts to {top} mph. Use care with high-profile trucks, especially on bridges."}
    if h == "refreeze":
        lo = min(s["amount"] for s in spans)
        return {"title": "Refreeze possible",
                "detail": f"Down to {lo}° after recent precipitation. Watch for black ice on untreated lots and roads."}
    return {"title": h.capitalize(), "detail": ""}


def run_severity(run, spans_by_key, day_start):
    """A flagged run is significant if it passes through a significant hazard."""
    if run["status"] != "flag":
        return None
    for p in run["points"]:
        if p["status"] != "flag":
            continue
        for s in spans_by_key.get(p["id"], []):
            if s["level"] == "flag" and s["severity"] == "significant":
                return "significant"
    return "minor"


def to_json(e):
    out = {k: v for k, v in e.items() if k not in ("start", "end")}
    out["start"] = e["start"].isoformat(timespec="minutes")
    out["end"] = e["end"].isoformat(timespec="minutes")
    return out
