"""
Mosaic Weather Advisory and Alert System: forecast fetcher

Reads config/network.yaml, pulls an hourly forecast for every location and
route checkpoint, plus active winter alerts, and saves one tidy file:
output/forecast.json

Primary source: National Weather Service (api.weather.gov)
Backup source:  Open-Meteo (used per point if the Weather Service fails)

Run it with:  python src/fetch_forecast.py
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "network.yaml"
OUTPUT_PATH = ROOT / "output" / "forecast.json"

MM_PER_INCH = 25.4
NWS_BASE = "https://api.weather.gov"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# NWS "weather" layer types we care about
ICE_TYPES = {"freezing_rain", "freezing_drizzle", "sleet", "freezing_spray"}
SNOW_TYPES = {"snow", "snow_showers", "blowing_snow"}

# Open-Meteo weather codes (WMO) that mean freezing precipitation
OM_ICE_CODES = {56, 57, 66, 67}
OM_SNOW_CODES = {71, 73, 75, 77, 85, 86}

WINTER_ALERTS_DEFAULT = {
    "Ice Storm Warning", "Winter Storm Warning",
    "Winter Storm Watch", "Winter Weather Advisory",
}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def all_points(cfg):
    """Every place we need a forecast for: stores, the CSC and checkpoints."""
    points = {}
    for key, loc in cfg["locations"].items():
        points[key] = {"name": loc["name"], "lat": loc["lat"], "lon": loc["lon"], "kind": "location"}
    for key, cp in cfg.get("checkpoints", {}).items():
        points[key] = {"name": cp["name"], "lat": cp["lat"], "lon": cp["lon"], "kind": "checkpoint"}
    return points


def get_json(session, url, params=None, tries=3, wait=4):
    """GET with a few retries, since the Weather Service is sometimes slow."""
    last_err = None
    for attempt in range(tries):
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 200:
                return r.json()
            last_err = f"HTTP {r.status_code} from {url}"
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(wait * (attempt + 1))
    raise RuntimeError(last_err)


_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")


def split_valid_time(valid_time):
    """'2026-11-16T12:00:00+00:00/PT6H' -> (start datetime UTC, hours)"""
    start_s, dur_s = valid_time.split("/")
    start = datetime.fromisoformat(start_s).astimezone(timezone.utc)
    m = _DURATION.fullmatch(dur_s)
    days, hours, _mins = (int(x) if x else 0 for x in m.groups())
    return start, max(1, days * 24 + hours)


def to_inches(value, uom):
    if value is None:
        return 0.0
    if uom and uom.endswith("mm"):
        return value / MM_PER_INCH
    if uom and uom.endswith(":m"):
        return value * 1000 / MM_PER_INCH
    return value


def to_f(value, uom):
    if value is None:
        return None
    if uom and "degC" in uom:
        return value * 9 / 5 + 32
    return value


# ----------------------------------------------------------------------
# National Weather Service
# ----------------------------------------------------------------------
def nws_hourly(session, lat, lon, tz):
    """Hourly series from the NWS forecast grid for one point."""
    pt = get_json(session, f"{NWS_BASE}/points/{lat:.4f},{lon:.4f}")
    grid = get_json(session, pt["properties"]["forecastGridData"])["properties"]

    hours = {}  # UTC hour -> dict

    def slot(dt):
        return hours.setdefault(dt, {"temp_f": None, "pop": 0, "precip_in": 0.0,
                                     "snow_in": 0.0, "ice_in": 0.0, "types": set(),
                                     "gust_mph": None, "sky": None})

    def each_hour(layer):
        data = grid.get(layer) or {}
        uom = data.get("uom", "")
        for v in data.get("values", []):
            start, n = split_valid_time(v["validTime"])
            for i in range(n):
                yield start + timedelta(hours=i), v["value"], uom, n

    for dt, val, uom, _ in each_hour("temperature"):
        slot(dt)["temp_f"] = to_f(val, uom)
    for dt, val, uom, _ in each_hour("probabilityOfPrecipitation"):
        slot(dt)["pop"] = val or 0
    for dt, val, uom, _ in each_hour("skyCover"):
        slot(dt)["sky"] = val
    # Amount layers are totals over the period, so spread them evenly by hour
    for layer, field in (("quantitativePrecipitation", "precip_in"),
                         ("snowfallAmount", "snow_in"),
                         ("iceAccumulation", "ice_in")):
        for dt, val, uom, n in each_hour(layer):
            slot(dt)[field] += to_inches(val, uom) / n
    for dt, val, uom, _ in each_hour("windGust"):
        if val is not None:
            mph = val * 0.621371 if "km_h" in uom else val
            slot(dt)["gust_mph"] = mph
    for dt, val, _uom, _ in each_hour("weather"):
        for w in val or []:
            if w and w.get("weather"):
                slot(dt)["types"].add(w["weather"])

    return finish_series(hours, tz)


def nws_alerts(session, lat, lon, wanted):
    data = get_json(session, f"{NWS_BASE}/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"})
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {})
        if p.get("event") in wanted:
            out.append({
                "event": p.get("event"),
                "headline": p.get("headline"),
                "onset": p.get("onset") or p.get("effective"),
                "ends": p.get("ends") or p.get("expires"),
            })
    return out


# ----------------------------------------------------------------------
# Open-Meteo (backup)
# ----------------------------------------------------------------------
def open_meteo_hourly(session, points, tz_name, days):
    """One request for many points. Returns {key: series}."""
    keys = list(points)
    params = {
        "latitude": ",".join(f"{points[k]['lat']:.4f}" for k in keys),
        "longitude": ",".join(f"{points[k]['lon']:.4f}" for k in keys),
        "hourly": "temperature_2m,precipitation_probability,precipitation,"
                  "snowfall,weather_code,wind_gusts_10m,cloud_cover",
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "wind_speed_unit": "mph",
        "timezone": "UTC",
        "forecast_days": days + 1,
    }
    data = get_json(session, OPEN_METEO_URL, params=params)
    if isinstance(data, dict):
        data = [data]
    tz = ZoneInfo(tz_name)
    result = {}
    for key, block in zip(keys, data):
        h = block["hourly"]
        hours = {}
        for i, t in enumerate(h["time"]):
            dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
            code = h["weather_code"][i]
            types = set()
            if code in OM_ICE_CODES:
                types.add("freezing_rain")
            elif code in OM_SNOW_CODES:
                types.add("snow")
            hours[dt] = {
                "temp_f": h["temperature_2m"][i],
                "pop": h["precipitation_probability"][i] or 0,
                "precip_in": h["precipitation"][i] or 0.0,
                # Open-Meteo reports snowfall in cm natively; with
                # precipitation_unit=inch it should return inches.
                # Verify against a known snow event before trusting it.
                "snow_in": h["snowfall"][i] or 0.0,
                "ice_in": 0.0,  # not provided by Open-Meteo
                "types": types,
                "gust_mph": h["wind_gusts_10m"][i],
                "sky": (h.get("cloud_cover") or [None] * len(h["time"]))[i],
            }
        result[key] = finish_series(hours, tz)
    return result


# ----------------------------------------------------------------------
# Shared output shape
# ----------------------------------------------------------------------
def finish_series(hours, tz):
    """Sort by time, convert to local time, round, make JSON friendly."""
    out = []
    for dt in sorted(hours):
        h = hours[dt]
        out.append({
            "time": dt.astimezone(tz).isoformat(timespec="minutes"),
            "temp_f": None if h["temp_f"] is None else round(h["temp_f"]),
            "pop": round(h["pop"] or 0),
            "precip_in": round(h["precip_in"], 3),
            "snow_in": round(h["snow_in"], 2),
            "ice_in": round(h["ice_in"], 3),
            "ice": bool(h["types"] & ICE_TYPES) or h["ice_in"] > 0,
            "snow": bool(h["types"] & SNOW_TYPES) or h["snow_in"] > 0,
            "types": sorted(h["types"]),
            "gust_mph": None if h["gust_mph"] is None else round(h["gust_mph"]),
            "sky": None if h.get("sky") is None else round(h["sky"]),
        })
    return out


def trim(series, start, end):
    return [h for h in series if start <= datetime.fromisoformat(h["time"]) < end]


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    cfg = load_config()
    tz = ZoneInfo(cfg.get("timezone", "America/New_York"))
    days = int(cfg.get("forecast_days", 3))
    ua = cfg.get("sources", {}).get("nws_user_agent", "Mosaic Weather System")
    wanted = set(cfg.get("rules", {}).get("official_alert", {}).get("events", WINTER_ALERTS_DEFAULT))

    session = requests.Session()
    session.headers.update({"User-Agent": ua, "Accept": "application/geo+json"})

    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days + 1)  # extra day so overnight runs are covered

    points = all_points(cfg)
    results, failed = {}, []

    for key, p in points.items():
        try:
            series = nws_hourly(session, p["lat"], p["lon"], tz)
            alerts = nws_alerts(session, p["lat"], p["lon"], wanted)
            results[key] = {**p, "source": "nws", "alerts": alerts, "hours": trim(series, start, end)}
            print(f"  NWS ok      {p['name']}")
        except Exception as e:  # noqa: BLE001 - any failure falls back to backup
            print(f"  NWS failed  {p['name']}: {e}")
            failed.append(key)
        time.sleep(0.5)  # be polite to the Weather Service

    if failed:
        try:
            backup = open_meteo_hourly(session, {k: points[k] for k in failed}, cfg["timezone"], days)
            for key in failed:
                results[key] = {**points[key], "source": "open_meteo", "alerts": [],
                                "hours": trim(backup[key], start, end)}
                print(f"  Backup ok   {points[key]['name']}")
        except Exception as e:  # noqa: BLE001
            print(f"  Backup failed too: {e}")

    missing = [points[k]["name"] for k in points if k not in results]
    output = {
        "generated": now.isoformat(timespec="minutes"),
        "timezone": cfg["timezone"],
        "sources_used": sorted({r["source"] for r in results.values()}),
        "missing": missing,
        "points": results,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=1), encoding="utf-8")

    print(f"\nSaved {len(results)} of {len(points)} points to {OUTPUT_PATH.relative_to(ROOT)}")
    if missing:
        print("Missing:", ", ".join(missing))
        sys.exit(1)


if __name__ == "__main__":
    main()
