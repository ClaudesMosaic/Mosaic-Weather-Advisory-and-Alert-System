"""
Mosaic Weather Advisory and Alert System: rules engine

Reads config/network.yaml and output/forecast.json, applies the rules, and
saves output/results.json, which the email and weather page are built from.

  Days 1 to 3: flags for every location (ice, alerts, rush-hour snow, heavy
               snow) and every truck run, checked stretch by stretch at the
               hours each truck is expected there.
  Days 4 to 7: "What's coming" outlook, a green / yellow / amber level per
               location with a one-line note. No flags, no truck timing.

Run it with:  python src/evaluate.py
"""

import json
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import changes
import summary
import events as ev_mod

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "network.yaml"
FORECAST_PATH = ROOT / "output" / "forecast.json"
RESULTS_PATH = ROOT / "output" / "results.json"

RANK = {"clear": 0, "watch": 1, "flag": 2}
OLEVEL = {"g": 0, "y": 1, "a": 2}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FLAG_DAYS = 3


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def worse(a, b):
    return a if RANK[a] >= RANK[b] else b


def clock(dt):
    """datetime -> '7:30 PM' (and 'midnight' / 'noon')"""
    if dt.minute == 0 and dt.hour in (0, 12):
        return "midnight" if dt.hour == 0 else "noon"
    return dt.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def span(a, b):
    return f"{clock(a)} to {clock(b)}"


def q15(dt, up=False):
    """Round a time to the quarter hour, so windows read cleanly."""
    m = dt.minute % 15
    if m == 0 and dt.second == 0:
        return dt.replace(second=0, microsecond=0)
    base = dt - timedelta(minutes=m, seconds=dt.second, microseconds=dt.microsecond)
    return base + timedelta(minutes=15) if up else base


def day_label(d):
    return d.strftime("%a, %b ") + str(d.day)


def parse_hm(s):
    h, m = s.split(":")
    return dtime(int(h), int(m))


class Point:
    """Hourly forecast for one place, with quick lookups by time range."""

    def __init__(self, key, data):
        self.key = key
        self.name = data["name"]
        self.source = data.get("source")
        self.hours = [{**h, "dt": datetime.fromisoformat(h["time"])} for h in data.get("hours", [])]
        self.alerts = []
        for a in data.get("alerts", []):
            try:
                onset = datetime.fromisoformat(a["onset"]) if a.get("onset") else None
                ends = datetime.fromisoformat(a["ends"]) if a.get("ends") else None
            except ValueError:
                onset = ends = None
            self.alerts.append({**a, "onset_dt": onset, "ends_dt": ends})

    def between(self, start, end):
        """Hours whose start falls inside [start, end), widened to the full hour."""
        s = start.replace(minute=0, second=0, microsecond=0)
        return [h for h in self.hours if s <= h["dt"] < end]

    def alerts_between(self, start, end, events=None):
        out = []
        for a in self.alerts:
            if events and a["event"] not in events:
                continue
            on = a["onset_dt"] or start
            off = a["ends_dt"] or end
            if on < end and off > start:
                out.append(a)
        return out


def ice_words(hours):
    types = {t for h in hours for t in h["types"]}
    words = []
    if "freezing_rain" in types:
        words.append("freezing rain")
    if "freezing_drizzle" in types:
        words.append("freezing drizzle")
    if "sleet" in types:
        words.append("sleet")
    return " and ".join(words) or "ice"


def conditions(hours):
    """A short conditions word for the forecast table."""
    if not hours:
        return "No data"
    ice = any(h["ice"] for h in hours)
    snow_total = sum(h["snow_in"] for h in hours)
    snowy = snow_total >= 0.1 or any(h["snow"] for h in hours)
    pop = max(h["pop"] for h in hours)
    if ice and snowy:
        return "Wintry mix"
    if ice:
        return ice_words([h for h in hours if h["ice"]]).capitalize()
    if snowy:
        return "Snow" if pop >= 60 else "Snow showers"
    if pop >= 60:
        return "Rain"
    if pop >= 30:
        return "Chance of rain"
    day = [h["sky"] for h in hours if h.get("sky") is not None and 9 <= h["dt"].hour <= 17]
    if not day:
        return "Dry"
    sky = sum(day) / len(day)
    if sky < 25:
        return "Sunny"
    if sky < 60:
        return "Partly cloudy"
    if sky < 85:
        return "Mostly cloudy"
    return "Cloudy"


def day_summary(hours):
    temps = [h["temp_f"] for h in hours if h["temp_f"] is not None]
    return {
        "conditions": conditions(hours),
        "high": max(temps) if temps else None,
        "low": min(temps) if temps else None,
        "pop": max((h["pop"] for h in hours), default=0),
        "rain_in": round(sum(h["precip_in"] for h in hours if not h["snow"] and not h["ice"]), 2),
        "snow_in": round(sum(h["snow_in"] for h in hours), 1),
        "ice_in": round(sum(h["ice_in"] for h in hours), 2),
    }


# ----------------------------------------------------------------------
# Settings with defaults (all optional in network.yaml)
# ----------------------------------------------------------------------
def extra_rules(cfg):
    r = cfg.get("rules", {})
    w = r.get("wind", {})
    rf = r.get("refreeze", {})
    return {
        "gust_watch": w.get("heads_up_gust_mph", 40),
        "gust_flag": w.get("flag_gust_mph", 55),
        "ice_sig": r.get("ice", {}).get("significant_inches", 0.10),
        "refreeze_on": rf.get("enabled", True),
        "refreeze_f": rf.get("max_temp_f", 32),
        "refreeze_back_h": rf.get("lookback_hours", 24),
        "refreeze_min_precip": rf.get("min_precip_inches", 0.05),
    }


def wet_before(pt, start, cfg):
    """Was there meaningful precipitation in the hours before `start`?"""
    x = extra_rules(cfg)
    prior = pt.between(start - timedelta(hours=x["refreeze_back_h"]), start)
    total = sum(h["precip_in"] for h in prior)
    return total >= x["refreeze_min_precip"] or any(h["snow"] or h["ice"] for h in prior)


def refreeze_hours(pt, hours, cfg):
    """Hours at or below freezing, with no active ice, after a wet stretch."""
    x = extra_rules(cfg)
    if not x["refreeze_on"] or not hours:
        return []
    cold = [h for h in hours if h["temp_f"] is not None and h["temp_f"] <= x["refreeze_f"] and not h["ice"] and not h["snow"]]
    return cold if cold and wet_before(pt, cold[0]["dt"], cfg) else []


# ----------------------------------------------------------------------
# Days 1 to 3: location flags
# ----------------------------------------------------------------------
def location_flags(pt, day_start, cfg):
    rules = cfg["rules"]
    hu = cfg.get("heads_up", {})
    frac = hu.get("near_threshold_fraction", 0.75) if hu.get("enabled", True) else None
    near_f = hu.get("near_freezing_f", 34)
    alert_events = set(rules.get("official_alert", {}).get("events", []))
    day_end = day_start + timedelta(days=1)
    hours = pt.between(day_start, day_end)
    flags = []

    # Ice: any freezing rain, freezing drizzle or sleet, any time of day
    ice_h = [h for h in hours if h["ice"]]
    if ice_h:
        amt = sum(h["ice_in"] for h in ice_h)
        extra = f", about {amt:.2f}\" of ice" if amt >= 0.01 else ""
        flags.append({"level": "flag", "rule": "Ice",
                      "when": span(ice_h[0]["dt"], ice_h[-1]["dt"] + timedelta(hours=1)),
                      "text": f"{ice_words(ice_h).capitalize()} in the forecast{extra}."})

    # Official winter alerts
    for a in pt.alerts_between(day_start, day_end, alert_events):
        on = max(a["onset_dt"] or day_start, day_start)
        off = min(a["ends_dt"] or day_end, day_end)
        flags.append({"level": "flag", "rule": "Official alert", "when": span(on, off),
                      "text": f"{a['event']} in effect."})

    # Rush-hour snow, plus near-freezing precipitation during the rush
    rmin = rules["rush_hour_snow"]["min_inches"]
    for wname in rules["rush_hour_snow"].get("windows", ["am_rush", "pm_rush"]):
        w = cfg["windows"][wname]
        ws = datetime.combine(day_start.date(), parse_hm(w["start"]), day_start.tzinfo)
        we = datetime.combine(day_start.date(), parse_hm(w["end"]), day_start.tzinfo)
        wh = pt.between(ws, we)
        label = "AM rush" if wname == "am_rush" else "PM rush"
        snow = sum(h["snow_in"] for h in wh)
        if snow >= rmin:
            flags.append({"level": "flag", "rule": "Rush-hour snow", "when": f"{label}, {span(ws, we)}",
                          "text": f"About {snow:.1f}\" of snow during the {label.split()[0]} commute."})
        elif frac and snow >= rmin * frac:
            flags.append({"level": "watch", "rule": "Light snow", "when": label,
                          "text": f"About {snow:.1f}\" of snow, just under the {rmin}\" rush-hour flag."})
        elif hu.get("enabled", True):
            cold_wet = [h for h in wh if h["pop"] >= 40 and h["temp_f"] is not None
                        and h["temp_f"] <= near_f and not h["ice"]]
            if cold_wet:
                lo = min(h["temp_f"] for h in cold_wet)
                flags.append({"level": "watch", "rule": "Near freezing", "when": label,
                              "text": f"Precipitation likely with temperatures near {lo}°. Watch for slick spots."})

    # Refreeze during the AM rush, the morning after rain, snow or ice
    w = cfg["windows"]["am_rush"]
    ws = datetime.combine(day_start.date(), parse_hm(w["start"]), day_start.tzinfo)
    we = datetime.combine(day_start.date(), parse_hm(w["end"]), day_start.tzinfo)
    rz = refreeze_hours(pt, pt.between(ws, we), cfg)
    if rz and not any(f["rule"] == "Ice" for f in flags):
        lo = min(h["temp_f"] for h in rz)
        flags.append({"level": "watch", "rule": "Refreeze possible", "when": "AM rush",
                      "text": f"Temperatures down to {lo}° after recent precipitation. Watch for black ice on untreated surfaces."})

    # Wind gusts
    x = extra_rules(cfg)
    gusts = [h for h in hours if h.get("gust_mph") is not None and h["gust_mph"] >= x["gust_watch"]]
    if gusts:
        top = max(h["gust_mph"] for h in gusts)
        lvl = "flag" if top >= x["gust_flag"] else "watch"
        flags.append({"level": lvl, "rule": "High wind" if lvl == "flag" else "Gusty winds",
                      "when": span(gusts[0]["dt"], gusts[-1]["dt"] + timedelta(hours=1)),
                      "text": f"Gusts to {top} mph. Use care with high-profile trucks."})

    # Heavy snow: 3"+ in any 12 hours, or 1"+ in a single hour
    hs = rules["heavy_snow"]
    for i, h in enumerate(hours):
        window = [x for x in pt.between(h["dt"], h["dt"] + timedelta(hours=12))]
        total = sum(x["snow_in"] for x in window)
        if total >= hs["min_inches_12h"] or h["snow_in"] >= hs["min_inches_1h"]:
            peak = max(x["snow_in"] for x in window)
            rate = f" Up to {peak:.1f}\" in a single hour." if peak >= hs["min_inches_1h"] else ""
            first = next((x for x in window if x["snow_in"] > 0), h)
            flags.append({"level": "flag", "rule": "Heavy snow",
                          "when": f"Starting around {clock(first['dt'])}",
                          "text": f"About {total:.1f}\" of snow over the next 12 hours.{rate}"})
            break

    status = "clear"
    for f in flags:
        status = worse(status, f["level"])
        f["when"] = f["when"][:1].upper() + f["when"][1:]
    return status, flags, day_summary(hours)


# ----------------------------------------------------------------------
# Days 1 to 3: truck runs, stretch by stretch
# ----------------------------------------------------------------------
def check_point_window(pt, start, end, cfg):
    rules = cfg["rules"]
    hu = cfg.get("heads_up", {})
    frac = hu.get("near_threshold_fraction", 0.75) if hu.get("enabled", True) else None
    near_f = hu.get("near_freezing_f", 34)
    alert_events = set(rules.get("official_alert", {}).get("events", []))
    hours = pt.between(start, end)
    reasons, status = [], "clear"

    if any(h["ice"] for h in hours):
        reasons.append(f"{ice_words([h for h in hours if h['ice']]).capitalize()} expected")
        status = "flag"
    snow = sum(h["snow_in"] for h in hours)
    tmin = rules["truck_snow"]["min_inches"]
    if snow >= tmin or any(h["snow_in"] >= rules["heavy_snow"]["min_inches_1h"] for h in hours):
        reasons.append(f"About {snow:.1f}\" of snow")
        status = "flag"
    elif frac and snow >= tmin * frac:
        reasons.append(f"About {snow:.1f}\" of snow, near the {tmin}\" flag")
        status = worse(status, "watch")
    for a in pt.alerts_between(start, end, alert_events):
        reasons.append(f"{a['event']} in effect")
        status = "flag"
    x = extra_rules(cfg)
    top = max((h["gust_mph"] for h in hours if h.get("gust_mph") is not None), default=0)
    if top >= x["gust_flag"]:
        reasons.append(f"Gusts to {top} mph")
        status = "flag"
    elif top >= x["gust_watch"]:
        reasons.append(f"Gusts to {top} mph")
        status = worse(status, "watch")
    if status == "clear" and hu.get("enabled", True):
        if any(h["pop"] >= 40 and h["temp_f"] is not None and h["temp_f"] <= near_f for h in hours):
            reasons.append("Precipitation near freezing")
            status = "watch"
        elif refreeze_hours(pt, hours, cfg):
            reasons.append("Possible refreeze after recent precipitation")
            status = "watch"
    if not hours:
        reasons.append("No forecast data for this time")
    return status, reasons


def run_on_day(run, day_start, points, cfg):
    tz = day_start.tzinfo
    smin = cfg.get("stop_minutes", {}).get("min", 30)
    smax = cfg.get("stop_minutes", {}).get("max", 60)
    flex = run.get("depart_flex_min", 0)
    early = datetime.combine(day_start.date(), parse_hm(run["depart"]), tz)
    late = early + timedelta(minutes=flex)
    pad = timedelta(minutes=15)
    out, legs = [], run["legs"]

    def add(key, ws, we):
        pt = points.get(key)
        if pt is None:
            out.append({"id": key, "name": key, "status": "clear", "window": span(ws, we),
                        "reasons": ["Not in forecast data"]})
            return
        st, why = check_point_window(pt, ws, we, cfg)
        out.append({"id": key, "name": pt.name, "status": st, "window": span(q15(ws), q15(we, True)), "reasons": why,
                    "start": q15(ws).isoformat(timespec="minutes"), "end": q15(we, True).isoformat(timespec="minutes")})

    add(legs[0]["from"], early - pad, late + pad)
    for i, leg in enumerate(legs):
        drive = timedelta(minutes=leg["drive_min"])
        via = leg.get("via", [])
        for j, cp in enumerate(via):
            f = (j + 1) / (len(via) + 1)
            add(cp, early + drive * f - 2 * pad, late + drive * f + 2 * pad)
        arr_e, arr_l = early + drive, late + drive
        last = i == len(legs) - 1
        add(leg["to"], arr_e - pad, arr_l + (pad if last else timedelta(minutes=smax) + pad))
        early, late = arr_e + timedelta(minutes=smin), arr_l + timedelta(minutes=smax)

    status = "clear"
    for p in out:
        status = worse(status, p["status"])
    return {"status": status, "points": out}


# ----------------------------------------------------------------------
# Days 4 to 7: outlook
# ----------------------------------------------------------------------
def outlook_for(pt, day_start):
    day_end = day_start + timedelta(days=1)
    hours = pt.between(day_start, day_end)
    later = pt.between(day_end, day_end + timedelta(hours=12))
    if not hours:
        return {"level": "g", "note": "Forecast not available yet.", "no_data": True, **day_summary(hours)}

    wet = [h for h in hours if h["pop"] >= 40]
    snowy = any(h["snow"] for h in hours) or sum(h["snow_in"] for h in hours) >= 0.05
    icy = any(h["ice"] for h in hours)
    cold_wet = [h for h in wet if h["temp_f"] is not None and h["temp_f"] <= 32]
    cool_wet = [h for h in wet if h["temp_f"] is not None and h["temp_f"] <= 36]
    watch_alert = pt.alerts_between(day_start, day_end, {"Winter Storm Watch"})
    refreeze = bool(wet) and any(h["temp_f"] is not None and h["temp_f"] <= 32
                                 for h in hours[hours.index(wet[-1]):] + later)

    if icy or watch_alert or (snowy and wet) or cold_wet:
        bits = []
        if icy:
            bits.append(f"possible {ice_words([h for h in hours if h['ice']])}")
        if snowy:
            bits.append("possible snow")
        if not bits:
            bits.append("precipitation possible with temperatures at or below freezing")
        if watch_alert:
            bits.append("Winter Storm Watch issued")
        when = (wet or hours)[0]["dt"]
        part = ("overnight" if when.hour < 6 else "in the morning" if when.hour < 12
                else "in the afternoon" if when.hour < 18 else "at night")
        note = f"{'; '.join(bits).capitalize()}, starting {part}."
        level = "a"
    elif refreeze:
        level, note = "y", "Precipitation, then temperatures dropping to freezing. Watch for refreeze."
    elif cool_wet or snowy:
        lo = min(h["temp_f"] for h in (cool_wet or hours) if h["temp_f"] is not None)
        level, note = "y", f"Precipitation likely with temperatures near {lo}°."
    else:
        level, note = "g", "No hazards expected."
    return {"level": level, "note": note, **day_summary(hours)}


def outlook_summary(locs, names):
    if locs and all(o.get("no_data") for o in locs.values()):
        return "Forecast not available yet for this day."
    by = {"a": [], "y": [], "g": []}
    for k, o in locs.items():
        by[o["level"]].append(names[k])
    highs = [o["high"] for o in locs.values() if o["high"] is not None]
    temps = f" Highs {min(highs)}° to {max(highs)}°." if highs else ""
    if by["a"]:
        s = f"Potential hazard at {', '.join(by['a'])}."
        if by["y"]:
            s += f" Worth watching at {', '.join(by['y'])}."
        return s + temps
    if by["y"]:
        return f"Worth watching at {', '.join(by['y'])}." + temps
    return "No hazards expected across the network." + temps


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    fc = json.loads(FORECAST_PATH.read_text(encoding="utf-8"))
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    total_days = int(cfg.get("forecast_days", 7))
    generated = datetime.fromisoformat(fc["generated"])
    today = generated.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)

    points = {k: Point(k, v) for k, v in fc["points"].items()}
    locs = cfg["locations"]
    names = {k: v["name"] for k, v in locs.items()}

    flag_days, outlook_days = [], []
    for n in range(total_days):
        d = today + timedelta(days=n)
        label = day_label(d)
        if n < FLAG_DAYS:
            day = {"date": d.date().isoformat(), "label": label, "locations": {}, "runs": []}
            for k in locs:
                pt = points.get(k)
                if pt is None:
                    day["locations"][k] = {"name": names[k], "status": "clear", "flags": [],
                                           "summary": day_summary([]), "missing": True}
                    continue
                st, fl, summ = location_flags(pt, d, cfg)
                day["locations"][k] = {"name": names[k], "state": locs[k]["state"],
                                       "status": st, "flags": fl, "summary": summ}
            wd = WEEKDAYS[d.weekday()]
            for rk, run in cfg.get("runs", {}).items():
                if wd not in run.get("days", []):
                    continue
                r = run_on_day(run, d, points, cfg)
                day["runs"].append({"id": rk, "name": run["name"], "operator": run.get("operator", ""),
                                    "depart": run["depart"], **r})
            flag_days.append(day)
        else:
            ol = {k: outlook_for(points[k], d) for k in locs if k in points}
            top = max((o["level"] for o in ol.values()), key=OLEVEL.get, default="g")
            outlook_days.append({
                "date": d.date().isoformat(), "label": label,
                "confidence": "Medium" if n < 5 else "Low",
                "level": top, "summary": outlook_summary(ol, names), "locations": ol,
            })

    # Events: one storm, one time span, every location it touches
    win_start, win_end = today, today + timedelta(days=FLAG_DAYS)
    x = extra_rules(cfg)
    spans = {k: ev_mod.point_spans(pt, win_start, win_end, cfg, x, refreeze_hours) for k, pt in points.items()}
    events = ev_mod.build_events({k: spans[k] for k in locs if k in spans}, cfg)
    for e in events:
        e["days"] = [i for i, d in enumerate(flag_days)
                     if e["start"] < datetime.fromisoformat(d["date"]).replace(tzinfo=tz) + timedelta(days=1)
                     and e["end"] > datetime.fromisoformat(d["date"]).replace(tzinfo=tz)]

    run_events = []
    for i, day in enumerate(flag_days):
        for r in day["runs"]:
            r["severity"] = None
            if r["status"] == "clear":
                continue
            bad = [p for p in r["points"] if p["status"] == r["status"]]
            sig = False
            for p in bad:
                ps, pe = datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"])
                sig |= any(s["level"] == "flag" and s["severity"] == "significant" and s["start"] < pe and s["end"] > ps
                           for s in spans.get(p["id"], []))
            r["severity"] = ("significant" if sig else "minor") if r["status"] == "flag" else None
            why = []
            for p in bad:
                for reason in p["reasons"]:
                    reason = reason.lower()
                    if ("near the" in reason and r["status"] == "flag") or reason in why:
                        continue
                    why.append(reason)
            first, last = bad[0], bad[-1]
            where = first["name"] if first["name"] == last["name"] else f"{first['name']} to {last['name']}"
            r["trouble"] = {"where": where, "from": first["window"].split(" to ")[0],
                            "to": last["window"].split(" to ")[-1], "why": why}
            run_events.append({"day": i, "date": day["date"], "label": day["label"], "id": r["id"], "name": r["name"],
                               "operator": r["operator"], "depart": r["depart"], "level": r["status"],
                               "severity": r["severity"], **r["trouble"]})

    results = {
        "generated": fc["generated"],
        "events": [ev_mod.to_json(e) for e in events],
        "run_events": run_events,
        "timezone": cfg["timezone"],
        "sources_used": fc.get("sources_used", []),
        "missing": fc.get("missing", []),
        "flag_days": flag_days,
        "outlook_days": outlook_days,
    }
    changes.compare(results, generated)
    results["bottom_line"], results["bottom_line_by_ai"] = summary.bottom_line(results)
    RESULTS_PATH.write_text(json.dumps(results, indent=1), encoding="utf-8")

    # Short readable log for the GitHub run page
    for day in flag_days:
        loc_flags = [f"{v['name']} ({', '.join(f['rule'] for f in v['flags'] if f['level'] == 'flag')})"
                     for v in day["locations"].values() if v["status"] == "flag"]
        run_flags = [r["name"] for r in day["runs"] if r["status"] == "flag"]
        print(f"{day['label']}: {len(loc_flags)} location flags, {len(run_flags)} runs flagged")
        for s in loc_flags + [f"Run: {r}" for r in run_flags]:
            print(f"    {s}")
    print(f"\nBottom line{' (AI)' if results['bottom_line_by_ai'] else ''}: {results['bottom_line']}")
    print("\nEvents:")
    for e in events:
        sev = e["severity"] or "heads-up"
        print(f"  [{sev}] {e['title']}: {e['regions']} ({', '.join(e['names'])}), {e['when']}. {e['detail']}"
              + (f" Alerts: {', '.join(e['alerts'])}." if e["alerts"] else ""))
    for r in run_events:
        print(f"  [run {r['severity'] or 'heads-up'}] {r['label']} {r['name']}: {r['where']}, {r['from']} to {r['to']}: {', '.join(r['why'])}")
    for o in outlook_days:
        print(f"{o['label']} outlook [{o['level']}] {o['summary']}")
    print(f"\nSaved {RESULTS_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
