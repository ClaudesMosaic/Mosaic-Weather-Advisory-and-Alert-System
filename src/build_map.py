"""
Mosaic Weather Advisory and Alert System: weather map page

Reads config/network.yaml, output/results.json and assets/basemap.svg and
writes output/weather-map.html, a single self-contained page that gets
attached to the morning email.

Layout: the network is split into three map tiles (North, Central, South),
with the long-haul corridors between them drawn as schematic strips that
show when each truck passes each checkpoint. Every store, route and
checkpoint can be clicked for details.

Tabs, days and the Map / Details switch work without JavaScript, so the page
still reads correctly in Outlook's attachment preview. Clicking for details
and the light / dark switch need a browser.

Run it with:  python src/build_map.py
"""

import json
import math
from datetime import datetime
from html import escape
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "network.yaml"
RESULTS_PATH = ROOT / "output" / "results.json"
BASEMAP_PATH = ROOT / "assets" / "basemap.svg"
OUT_PATH = ROOT / "output" / "weather-map.html"

# Map projection (Lambert conformal conic fitted to VA, MD and NC; matches basemap.svg)
_K, _TX, _TY = 8384.375528670138, 455.94862046145386, 6220.087312673923
_Y0, _Y1 = math.radians(36), math.radians(39)
_tany = lambda y: math.tan((math.pi / 2 + y) / 2)  # noqa: E731
_N = math.log(math.cos(_Y0) / math.cos(_Y1)) / math.log(_tany(_Y1) / _tany(_Y0))
_F = math.cos(_Y0) * _tany(_Y0) ** _N / _N


def project(lat, lon):
    x, y = math.radians(lon + 78), math.radians(lat)
    r = _F / _tany(y) ** _N
    return _TX + _K * r * math.sin(_N * x), _TY - _K * (_F - r * math.cos(_N * x))


# ----------------------------------------------------------------------
# The three tiles and the corridors between them.
# Optional override in network.yaml under `map:` with the same shape.
# ----------------------------------------------------------------------
PANEL_W = 640
DEFAULT_MAP = {
    "regions": [
        {"id": "north", "title": "North", "sub": "NOVA and Maryland", "height": 360, "pad": 12,
         "stores": ["chantilly", "fairfax", "alexandria", "nbethesda", "owings"],
         "fit": ["spring", "tysons", "i66", "cpark", "b695"]},
        {"id": "central", "title": "Central", "sub": "Doswell, Richmond and Hampton Roads", "height": 300, "pad": 30,
         "stores": ["csc", "richmond", "vabeach"], "fit": ["rva95", "wburg", "hrbt"]},
        {"id": "south", "title": "South", "sub": "North Carolina", "height": 240, "pad": 28,
         "stores": ["raleigh", "greensboro"], "fit": ["durham", "burl", "hend", "rockymt"]},
    ],
    "corridors": [
        {"after": "north", "title": "I-95 corridor", "rows": [
            {"label": "Doswell to NOVA", "from": "Doswell", "to": "NOVA", "nodes": ["fred", "wood"]}]},
        {"after": "central", "title": "Southern corridor", "rows": [
            {"label": "Outbound", "from": "Virginia Beach", "to": "Raleigh", "nodes": ["suffolk", "emporia", "rockymt"]},
            {"label": "Return", "from": "Raleigh", "to": "Doswell", "nodes": ["hend", "shill", "ptrsbg"]}]},
    ],
}
# Label placement for store names: dx, dy, anchor (in tile pixels)
LABEL_POS = {
    "chantilly": (-12, 4, "end"), "fairfax": (0, 24, "middle"), "alexandria": (12, 16, "start"),
    "nbethesda": (12, 4, "start"), "owings": (12, 4, "start"),
    "csc": (16, 5, "start"), "richmond": (-14, 5, "end"), "vabeach": (0, 26, "middle"),
    "raleigh": (14, 5, "start"), "greensboro": (0, -14, "middle"),
}
CAMERA_NAMES = {"VA": "VDOT 511", "MD": "MD CHART", "NC": "DriveNC"}
STATUS_WORD = {"flag": "Flagged", "watch": "Heads-up", "clear": "Clear", "idle": "No trucks"}
OUT_WORD = {"a": "Potential hazard", "y": "Worth watching", "g": "Nothing notable"}
RANK = {"idle": -1, "clear": 0, "watch": 1, "flag": 2}
SEVR = {None: 0, "minor": 1, "significant": 2}
SEVW = {None: "Heads-up", "minor": "Minor", "significant": "Significant"}


def e(s):
    return escape(str(s)) if s is not None else ""


def worst(statuses, default="clear"):
    return max(statuses, key=RANK.get, default=default)


def chg(c):
    return f'<span class="chg {c}">{c.capitalize()}</span>' if c in ("new", "worse", "better") else ""


def short_window(w):
    return w.replace(":00", "")


def sig_url(cfg, lat, lon, zoom):
    tpl = cfg.get("links", {}).get("sigalert")
    return tpl.format(lat=lat, lon=lon, zoom=zoom) if tpl else None


def links_html(cfg, k):
    loc = cfg["locations"].get(k) or cfg.get("checkpoints", {}).get(k, {})
    state = loc.get("state") or ("NC" if loc.get("lat", 99) < 36.55 else "MD" if loc.get("lat", 0) > 38.95 else "VA")
    cams = cfg.get("links", {}).get("cameras", {})
    parts = []
    if cams.get(state):
        parts.append(f'<a href="{e(cams[state])}" target="_blank" rel="noopener">{CAMERA_NAMES.get(state, "Cameras")}</a>')
    sig = sig_url(cfg, loc.get("lat"), loc.get("lon"), cfg.get("links", {}).get("sigalert_store_zoom", 4))
    if sig:
        parts.append(f'<a href="{e(sig)}" target="_blank" rel="noopener">SigAlert</a>')
    return " ".join(parts)


# ----------------------------------------------------------------------
# Tile geometry
# ----------------------------------------------------------------------
class Tile:
    """One region's map, with its own zoom, in tile pixel coordinates."""

    def __init__(self, reg, xy):
        pad = reg.get("pad", 16)
        pts = [xy[k] for k in reg["stores"] + reg.get("fit", []) if k in xy]
        x0, x1 = min(p[0] for p in pts) - pad, max(p[0] for p in pts) + pad
        y0, y1 = min(p[1] for p in pts) - pad, max(p[1] for p in pts) + pad
        self.w, self.h = reg.get("width", PANEL_W), reg.get("height", 300)
        self.z = min(self.w / (x1 - x0), self.h / (y1 - y0))
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.vx, self.vy = cx - self.w / 2 / self.z, cy - self.h / 2 / self.z

    def p(self, pt):
        return round((pt[0] - self.vx) * self.z, 1), round((pt[1] - self.vy) * self.z, 1)

    def inside(self, q, margin=0):
        return margin <= q[0] <= self.w - margin and margin <= q[1] <= self.h - margin

    def edge_hit(self, a, b):
        """Where the segment a (inside) to b (outside) leaves the tile."""
        best = None
        for t_edge, val, axis in ((0, 0, 0), (0, self.w, 0), (0, 0, 1), (0, self.h, 1)):
            d = b[axis] - a[axis]
            if d == 0:
                continue
            t = (val - a[axis]) / d
            if 0 < t <= 1:
                q = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                if -0.5 <= q[0] <= self.w + 0.5 and -0.5 <= q[1] <= self.h + 0.5 and (best is None or t < best[0]):
                    best = (t, q, axis, val)
        return best


def point_status(day, k):
    """Worst status of any truck passing a point that day ('idle' if none)."""
    st = [p["status"] for run in day["runs"] for p in run["points"] if p["id"] == k]
    return worst(st, "idle")


def tile_svg(tile, reg, cfg, xy, day, names, store_cls, store_tip, lanes=True):
    out = [f'<use href="#base" width="900" height="640" transform="translate({-tile.vx * tile.z:.1f},{-tile.vy * tile.z:.1f}) scale({tile.z:.3f})"/>']
    exits = {}
    if lanes and day is not None:
        segs = []
        for run in day["runs"]:
            pts = run["points"]
            for a, b in zip(pts, pts[1:]):
                if a["id"] not in xy or b["id"] not in xy:
                    continue
                pa, pb = tile.p(xy[a["id"]]), tile.p(xy[b["id"]])
                s = worst([a["status"], b["status"]])
                segs.append((RANK[s], s, pa, pb, run, a, b))
                ina, inb = tile.inside(pa), tile.inside(pb)
                if ina != inb:
                    inner, outer, o = (pa, pb, b) if ina else (pb, pa, a)
                    hit = tile.edge_hit(inner, outer)
                    if hit and o["id"] not in exits:
                        exits[o["id"]] = (hit, o["name"])
        segs.sort(key=lambda t: t[0])
        for _, s, pa, pb, run, a, b in segs:
            out.append(f'<path class="lane {s}" data-run="{run["id"]}" d="M{pa[0]},{pa[1]} L{pb[0]},{pb[1]}">'
                       f'<title>{e(run["name"])}: {e(a["name"])} to {e(b["name"])}, {STATUS_WORD[s].lower()}</title></path>')
        # Checkpoints inside this tile: diamonds, clickable
        for k, cp in cfg.get("checkpoints", {}).items():
            q = tile.p(xy[k])
            if not tile.inside(q, 4):
                continue
            s = point_status(day, k)
            out.append(f'<g class="wp {s}" data-k="{k}" tabindex="0" role="button" aria-label="{e(cp["name"])}, {STATUS_WORD[s].lower()}">'
                       f'<title>{e(cp["name"])}: {STATUS_WORD[s].lower()}</title>'
                       f'<rect class="hit" x="{q[0] - 11}" y="{q[1] - 11}" width="22" height="22"/>'
                       f'<rect class="dia" x="{q[0] - 5}" y="{q[1] - 5}" width="10" height="10" transform="rotate(45 {q[0]} {q[1]})"/></g>')
        # Where routes leave the tile
        for k, ((t, q, axis, val), name) in exits.items():
            x, y = q
            if axis == 1:  # top or bottom edge
                y = 16 if val == 0 else tile.h - 8
                anchor = "middle"
                arrow = "▲" if val == 0 else "▼"
                x = min(max(x, 60), tile.w - 60)
            else:
                x = 8 if val == 0 else tile.w - 8
                anchor = "start" if val == 0 else "end"
                arrow = "◀" if val == 0 else "▶"
                y = min(max(y, 18), tile.h - 10)
            label = name.replace("I-95 ", "").replace("I-85 ", "").replace("I-64 ", "").replace("US-58 ", "")
            text = f"{arrow} {label}" if anchor != "end" else f"{label} {arrow}"
            out.append(f'<text class="exit" x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}">{e(text)}</text>')
    # Stores
    for k in reg["stores"]:
        if k not in xy:
            continue
        x, y = tile.p(xy[k])
        dx, dy, anc = LABEL_POS.get(k, (12, 4, "start"))
        cls, tip = store_cls(k), store_tip(k)
        if k == "csc":
            out.append(f'<g class="loc {cls} csc" data-k="{k}" tabindex="0" role="button" aria-label="{e(tip)}"><title>{e(tip)}</title>'
                       f'<rect class="ring" x="{x - 17}" y="{y - 17}" width="34" height="34" rx="6"/>'
                       f'<rect class="hub" x="{x - 12}" y="{y - 12}" width="24" height="24" rx="3"/>'
                       f'<circle class="dot" cx="{x + 12}" cy="{y - 12}" r="6"/>'
                       f'<text class="tag" x="{x}" y="{y + 4}" text-anchor="middle">CSC</text>'
                       f'<text x="{x + 20}" y="{y + 5}">Doswell</text></g>')
        else:
            out.append(f'<g class="loc {cls}" data-k="{k}" tabindex="0" role="button" aria-label="{e(tip)}"><title>{e(tip)}</title>'
                       f'<circle class="ring" cx="{x}" cy="{y}" r="14"/><circle class="dot" cx="{x}" cy="{y}" r="8"/>'
                       f'<text x="{x + dx}" y="{y + dy}" text-anchor="{anc}">{e(names[k])}</text></g>')
    return (f'<svg class="tmap" viewBox="0 0 {tile.w} {tile.h}" role="img" aria-label="{e(reg["title"])} region map">'
            + "".join(out) + "</svg>")


# ----------------------------------------------------------------------
# Condition symbols and grouped stretches for truck runs
# ----------------------------------------------------------------------
ICON_DEFS = """
<symbol id="ic-snow" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
<path d="M12 2v20M3.3 7l17.4 10M3.3 17L20.7 7"/><path d="M9 4l3 2 3-2M9 20l3-2 3 2M4 10.5l2.8-.6-.9-2.9M20 13.5l-2.8.6.9 2.9M4 13.5l2.8.6-.9 2.9M20 10.5l-2.8-.6.9-2.9"/></g></symbol>
<symbol id="ic-ice" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
<path d="M12 3c3 4.2 5 7.2 5 9.6A5 5 0 0 1 7 12.6C7 10.2 9 7.2 12 3z"/></g><g fill="currentColor"><circle cx="5" cy="19.5" r="1.6"/><circle cx="12" cy="21" r="1.6"/><circle cx="19" cy="19.5" r="1.6"/></g></symbol>
<symbol id="ic-alert" viewBox="0 0 24 24"><path d="M12 3L22 20H2z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>
<path d="M12 9.5v5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><circle cx="12" cy="17.4" r="1.3" fill="currentColor"/></symbol>
<symbol id="ic-wind" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
<path d="M3 8h11a3 3 0 1 0-3-3"/><path d="M3 12h16a3 3 0 1 1-3 3"/><path d="M3 16h7"/></g></symbol>
<symbol id="ic-cold" viewBox="0 0 24 24"><g fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
<path d="M10 4a2 2 0 0 1 4 0v9.3a4 4 0 1 1-4 0z"/><path d="M12 8v7"/><path d="M17 6h3M17 9.5h2"/></g></symbol>
<symbol id="ic-ok" viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></symbol>
<symbol id="ic-none" viewBox="0 0 24 24"><path d="M6 12h12" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></symbol>
"""
ICON_NAME = {"snow": "Snow", "ice": "Ice or sleet", "alert": "Official alert", "wind": "Gusty wind",
             "cold": "Near freezing", "ok": "No concerns", "none": "No forecast data"}
ICON_ORDER = ["ice", "snow", "alert", "wind", "cold"]


def reason_kind(reason):
    r = reason.lower()
    if "in effect" in r:
        return "alert"
    if any(w in r for w in ("sleet", "freezing rain", "freezing drizzle", "ice")):
        return "ice"
    if "snow" in r:
        return "snow"
    if "gust" in r:
        return "wind"
    if "freezing" in r or "refreeze" in r:
        return "cold"
    if "no forecast" in r or "not in forecast" in r:
        return "none"
    return "cold"


def icon(kind, cls=""):
    return (f'<svg class="ic {kind} {cls}" aria-label="{ICON_NAME[kind]}" role="img"><title>{ICON_NAME[kind]}</title>'
            f'<use href="#ic-{kind}"/></svg>')


HAZARD_KINDS = {"snow": ["snow"], "ice": ["ice"], "mix": ["ice", "snow"], "wind": ["wind"], "refreeze": ["cold"], "alert": ["alert"]}


def event_icons(ev):
    kinds = list(HAZARD_KINDS.get(ev["hazard"], []))
    if ev.get("alerts") and "alert" not in kinds:
        kinds.append("alert")
    return "".join(icon(k) for k in ICON_ORDER if k in kinds)


def icons_for(reasons):
    kinds = {reason_kind(x) for x in reasons}
    return "".join(icon(k) for k in ICON_ORDER + ["none"] if k in kinds)


def stretches(points):
    """Group consecutive stops that share a status and the same kinds of conditions."""
    groups = []
    for p in points:
        kinds = frozenset(reason_kind(x) for x in p["reasons"] if reason_kind(x) != "alert") if p["status"] != "clear" else frozenset()
        key = (p["status"], kinds)
        if groups and groups[-1]["key"] == key:
            groups[-1]["points"].append(p)
        else:
            groups.append({"key": key, "points": [p]})
    for g in groups:
        ps = g["points"]
        g["status"] = ps[0]["status"]
        g["start"] = short_window(ps[0]["window"]).split(" to ")[0]
        g["end"] = short_window(ps[-1]["window"]).split(" to ")[-1]
        g["where"] = ps[0]["name"] if len(ps) == 1 else f'{ps[0]["name"]} to {ps[-1]["name"]}'
        why = []
        for p in ps:
            for x in p["reasons"]:
                x = x[:1].upper() + x[1:]
                if x not in why:
                    why.append(x)
        why.sort(key=lambda x: reason_kind(x) == "alert")
        g["why"] = why if g["status"] != "clear" else []
    return groups


def run_timeline(run):
    rows = []
    for g in stretches(run["points"]):
        s, ps = g["status"], g["points"]
        sym = icons_for(g["why"]) if g["why"] else icon("ok")
        why = f'<span class="tl-why">{e(". ".join(g["why"]))}.</span>' if g["why"] else ""
        if len(ps) > 1:
            stops = "".join(f'<li data-k="{p["id"]}">{e(p["name"])}<span>{e(short_window(p["window"]))}</span></li>' for p in ps)
            more = f'<details><summary>{len(ps)} stops</summary><ul class="tl-stops">{stops}</ul></details>'
            click = ""
        else:
            more, click = "", f' data-k="{ps[0]["id"]}"'
        rows.append(f'<li class="tl-row {s}"{click}><div class="tl-time"><b>{e(g["start"])}</b><span>to {e(g["end"])}</span></div>'
                    f'<div class="tl-body"><div class="tl-head"><span class="pill {s}">{STATUS_WORD[s]}</span>{sym}</div>'
                    f'<b class="tl-where">{e(g["where"])}</b>{why}{more}</div></li>')
    return f'<ol class="tl">{"".join(rows)}</ol>'


# ----------------------------------------------------------------------
# Overview (the zoomed-out network) and the zoom controls
# ----------------------------------------------------------------------
def overview_html(mp, cfg, xy, day, cls_of, tip_of, cards):
    """Whole network on one small map, regions outlined; click a region to zoom in."""
    every = [k for reg in mp["regions"] for k in reg["stores"]]
    ov = Tile({"stores": every, "fit": [], "width": 470, "height": 560, "pad": 34}, xy)
    ov.vy -= 16 / ov.z  # room for the North tab label
    out = [f'<use href="#base" width="900" height="640" transform="translate({-ov.vx * ov.z:.1f},{-ov.vy * ov.z:.1f}) scale({ov.z:.3f})"/>']
    for reg, (cls, _) in zip(mp["regions"], cards):
        pts = [ov.p(xy[k]) for k in reg["stores"] + reg.get("fit", []) if k in xy]
        x0, y0 = min(q[0] for q in pts) - 16, min(q[1] for q in pts) - 16
        x1, y1 = max(q[0] for q in pts) + 16, max(q[1] for q in pts) + 16
        out.append(f'<g class="hot {cls}" data-zoom="{reg["id"]}" tabindex="0" role="button" aria-label="Zoom in to {e(reg["title"])}">'
                   f'<title>Zoom in to {e(reg["title"])}</title>'
                   f'<rect x="{x0:.0f}" y="{y0:.0f}" width="{x1 - x0:.0f}" height="{y1 - y0:.0f}" rx="10"/>'
                   f'<text x="{x0 + 2:.0f}" y="{y0 - 7:.0f}">{e(reg["title"])}</text></g>')
    if day is not None:
        segs = []
        for run in day["runs"]:
            for a, b in zip(run["points"], run["points"][1:]):
                if a["id"] in xy and b["id"] in xy:
                    pa, pb = ov.p(xy[a["id"]]), ov.p(xy[b["id"]])
                    s = worst([a["status"], b["status"]])
                    segs.append((RANK[s], f'<path class="lane {s}" data-run="{run["id"]}" d="M{pa[0]},{pa[1]} L{pb[0]},{pb[1]}">'
                                          f'<title>{e(run["name"])}: {e(a["name"])} to {e(b["name"])}, {STATUS_WORD[s].lower()}</title></path>'))
        out += [h for _, h in sorted(segs, key=lambda x: x[0])]
    for k in every:
        x, y = ov.p(xy[k])
        shape = (f'<rect class="dot" x="{x - 6}" y="{y - 6}" width="12" height="12" rx="2"/>' if k == "csc"
                 else f'<circle class="dot" cx="{x}" cy="{y}" r="6"/>')
        out.append(f'<g class="loc {cls_of(k)}" data-k="{k}"><title>{e(tip_of(k))}</title>{shape}</g>')
    svg = f'<svg class="ovmap" viewBox="0 0 {ov.w} {ov.h}" role="img" aria-label="Network overview">{"".join(out)}</svg>'
    card_html = "".join(
        f'<div class="rcard {cls}" data-zoom="{reg["id"]}" tabindex="0" role="button">'
        f'<div class="rc-top"><b>{e(reg["title"])}</b>{badge}</div><span class="rc-sub">{e(reg["sub"])}</span>{line}'
        f'<span class="rc-go">Zoom in</span></div>'
        for reg, (cls, (badge, line)) in zip(mp["regions"], cards))
    return (f'<div class="overview"><div class="ov-map">{svg}</div><div class="ov-side">{card_html}'
            f'<p class="ov-hint">Select a region on the map or a card to zoom in.</p></div></div>')


def zoombar(mp):
    regs = "".join(f'<button type="button" data-zoom="{reg["id"]}">{e(reg["title"])}</button>' for reg in mp["regions"])
    return (f'<div class="zoombar"><button type="button" class="zout" data-zoom="all">Whole network</button>'
            f'<span class="zseg">{regs}</span></div>')


# ----------------------------------------------------------------------
# Day views
# ----------------------------------------------------------------------
def store_events(r, i, k):
    return [ev for ev in r.get("events", []) if i in ev.get("days", []) and k in ev["locations"]]


def readout(k, v, evs, names):
    s = v["summary"]
    hi = "–" if s["high"] is None else f'{s["high"]}°'
    lo = "–" if s["low"] is None else f'{s["low"]}°'
    if evs:
        ev = max(evs, key=lambda x: (RANK[x["level"]], SEVR[x["severity"]]))
        note = f'{ev["title"]}, {ev["when"]}'
        kinds = {k for x in evs for k in HAZARD_KINDS.get(x["hazard"], [])} | ({"alert"} if any(x.get("alerts") for x in evs) else set())
        sym = "".join(icon(k) for k in ICON_ORDER if k in kinds)
    else:
        note, sym = s["conditions"], ""
    return (f'<div class="ro {v["status"]}" data-k="{k}" tabindex="0" role="button">'
            f'<span class="ro-n">{e(names[k])}</span><span class="ro-t">{hi} / {lo}</span>'
            f'<span class="ro-c">{f"<span class=ics>{sym}</span> " if sym else ""}{e(note)}</span></div>')


def corridor_html(cor, day, cfg, mp):
    rows = []
    for row in cor["rows"]:
        nodes = []
        for k in row["nodes"]:
            cp = cfg.get("checkpoints", {}).get(k)
            if not cp:
                continue
            s = point_status(day, k)
            times = [(run["name"], p["window"]) for run in day["runs"] for p in run["points"] if p["id"] == k]
            t_html = ("".join(f'<span>{e(n)} {e(short_window(w))}</span>' for n, w in times)
                      if times else '<span>No trucks this day</span>')
            nodes.append(f'<div class="node {s}" data-k="{k}" tabindex="0" role="button">'
                         f'<i></i><b>{e(cp["name"].replace("I-95 ", "").replace("I-85 ", "").replace("US-58 ", ""))}</b>{t_html}</div>')
        rows.append(f'<div class="crow"><span class="clabel">{e(row["label"])}</span><span class="cend">{e(row["from"])}</span>'
                    f'<div class="track">{"".join(nodes)}</div><span class="cend">{e(row["to"])}</span></div>')
    st = worst([point_status(day, k) for row in cor["rows"] for k in row["nodes"]], "idle")
    ids = [reg["id"] for reg in mp["regions"]]
    nxt = ids[ids.index(cor["after"]) + 1] if cor["after"] in ids and ids.index(cor["after"]) + 1 < len(ids) else ""
    return (f'<div class="corridor {st}" data-regions="{cor["after"]} {nxt}"><div class="chead"><h3>{e(cor["title"])}</h3>'
            f'<span class="pill {st}">{STATUS_WORD[st]}</span></div>{"".join(rows)}</div>')


def details_table_flag(day, cfg):
    rows = []
    for k, v in day["locations"].items():
        s = v["summary"]
        hi = "–" if s["high"] is None else f'{s["high"]}°'
        lo = "–" if s["low"] is None else f'{s["low"]}°'
        rows.append(f'<tr class="{v["status"]}" data-k="{k}"><td><span class="dotk {v["status"]}"></span>{e(v["name"])}</td><td>{e(s["conditions"])}</td>'
                    f'<td class="n">{hi} / {lo}</td><td class="n">{s["pop"]}%</td><td class="n">{s["rain_in"]:.2f}"</td>'
                    f'<td class="n">{s["snow_in"]:.1f}"</td><td class="lk">{links_html(cfg, k)}</td></tr>')
    return (f'<div class="tablewrap"><table><thead><tr><th>Location</th><th>Conditions</th><th class="n">High / Low</th><th class="n">Precip</th>'
            f'<th class="n">Rain</th><th class="n">Snow</th><th>Traffic cameras</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def flag_view(i, day, cfg, xy, r, mp):
    L = day["locations"]
    names = {k: v["name"] for k, v in cfg["locations"].items()}

    # Side panel: events and runs for the day
    items = []
    for ev in r.get("events", []):
        if i not in ev.get("days", []):
            continue
        sev = ev["severity"] if ev["level"] == "flag" else None
        alerts = f' {", ".join(ev["alerts"])} in effect.' if ev.get("alerts") else ""
        items.append((RANK[ev["level"]] * 10 + SEVR[sev],
                      f'<li class="{ev["level"]}" data-k="{ev["locations"][0]}"><div class="tags"><span class="sev {sev or "hu"}">{SEVW[sev]}</span>{chg(ev.get("change"))}<span class="ics">{event_icons(ev)}</span></div>'
                      f'<b>{e(ev["title"])}: {e(ev["regions"])}</b><small class="when">{e(ev["when"])}</small>'
                      f'<small>{e(", ".join(ev["names"]))}. {e(ev["detail"])}{e(alerts)}</small></li>'))
    for re_ in r.get("run_events", []):
        if re_["day"] != i:
            continue
        sev = re_["severity"] if re_["level"] == "flag" else None
        items.append((RANK[re_["level"]] * 10 + SEVR[sev],
                      f'<li class="{re_["level"]}" data-run="{re_["id"]}"><div class="tags"><span class="sev {sev or "hu"}">{SEVW[sev]}</span>{chg(re_.get("change"))}<span class="ics">{icons_for(re_["why"])}</span></div>'
                      f'<b>{e(re_["name"])} run</b><small class="when">About {e(re_["from"])} to {e(re_["to"])}</small>'
                      f'<small>{e(re_["where"])}: {e(", ".join(re_["why"]))}.</small></li>'))
    items.sort(key=lambda t: -t[0])
    flag_list = (f'<ul class="flaglist">{"".join(h for _, h in items)}</ul>' if items
                 else '<p class="empty">No flags or heads-ups this day.</p>')

    runs, dets = [], []
    for run in day["runs"]:
        run_reasons = [x for p in run["points"] if p["status"] != "clear" for x in p["reasons"]]
        runs.append(f'<div class="runrow" data-run="{run["id"]}" tabindex="0" role="button"><span class="pill {run["status"]}">{STATUS_WORD[run["status"]]}</span>'
                    f'<b>{e(run["name"])}<span class="ics">{icons_for(run_reasons)}</span></b><span class="muted">leaves {e(run["depart_label"])}</span></div>')
        dets.append(f'<div class="det" data-key="r:{run["id"]}"><button class="back" type="button">Back to the day</button>'
                    f'<h2>{e(run["name"])}</h2><p><span class="pill {run["status"]}">{STATUS_WORD[run["status"]]}</span> '
                    f'<span class="muted">{e(run["operator"])}, leaves {e(run["depart_label"])}</span></p>'
                    f'<p class="muted small">When the truck is expected on each stretch. Stops with the same conditions are grouped.</p>{run_timeline(run)}</div>')
    for k, v in L.items():
        s = v["summary"]
        fl = "".join(f'<li class="{f["level"]}"><b>{e(f["rule"])}</b><small class="when">{e(f["when"])}</small><small>{e(f["text"])}</small></li>' for f in v["flags"])
        thru = [(run, p) for run in day["runs"] for p in run["points"] if p["id"] == k]
        thru_html = "".join(f'<li class="{p["status"]}"><span class="pill {p["status"]}">{STATUS_WORD[p["status"]]}</span> {e(run["name"])}'
                            f'<span class="muted"> {e(short_window(p["window"]))}</span></li>' for run, p in thru)
        hi = "–" if s["high"] is None else f'{s["high"]}°'
        lo = "–" if s["low"] is None else f'{s["low"]}°'
        dets.append(f'<div class="det" data-key="k:{k}"><button class="back" type="button">Back to the day</button>'
                    f'<h2>{e(v["name"])}</h2><p><span class="pill {v["status"]}">{STATUS_WORD[v["status"]]}</span></p>'
                    f'<dl class="facts"><dt>Conditions</dt><dd>{e(s["conditions"])}</dd><dt>High / low</dt><dd>{hi} / {lo}</dd>'
                    f'<dt>Precip chance</dt><dd>{s["pop"]}%</dd><dt>Rain</dt><dd>{s["rain_in"]:.2f}"</dd><dt>Snow</dt><dd>{s["snow_in"]:.1f}"</dd></dl>'
                    + (f'<h3>Flags</h3><ul class="flaglist">{fl}</ul>' if fl else "")
                    + (f'<h3>Trucks through here</h3><ul class="route">{thru_html}</ul>' if thru else "")
                    + f'<h3>Traffic cameras</h3><p class="lk">{links_html(cfg, k)}</p></div>')
    for k, cp in cfg.get("checkpoints", {}).items():
        thru = [(run, p) for run in day["runs"] for p in run["points"] if p["id"] == k]
        s = point_status(day, k)
        if thru:
            body = "".join(f'<li class="{p["status"]}"><span class="pill {p["status"]}">{STATUS_WORD[p["status"]]}</span> {e(run["name"])}'
                           f'<span class="muted"> {e(short_window(p["window"]))}</span>'
                           + (f'<small>{e(", ".join(p["reasons"]))}</small>' if p["reasons"] and p["status"] != "clear" else "") + '</li>'
                           for run, p in thru)
            body = f'<h3>Trucks passing</h3><ul class="route">{body}</ul>'
        else:
            body = '<p class="muted">No trucks are scheduled through here this day.</p>'
        dets.append(f'<div class="det" data-key="k:{k}"><button class="back" type="button">Back to the day</button>'
                    f'<h2>{e(cp["name"])}</h2><p><span class="pill {s}">{STATUS_WORD[s]}</span> <span class="muted">Route checkpoint</span></p>'
                    f'<p class="muted small">Checked for the hours each truck is expected to pass.</p>{body}'
                    f'<h3>Traffic cameras</h3><p class="lk">{links_html(cfg, k)}</p></div>')
    runs_html = "".join(runs) or '<p class="empty">No truck runs scheduled this day.</p>'

    # Board: tiles and corridors
    board = []
    for reg in mp["regions"]:
        tile = Tile(reg, xy)
        st = worst([L[k]["status"] for k in reg["stores"] if k in L])
        svg = tile_svg(tile, reg, cfg, xy, day, names, lambda k: L[k]["status"],
                       lambda k: f'{L[k]["name"]}: {STATUS_WORD[L[k]["status"]].lower()}')
        ros = "".join(readout(k, L[k], store_events(r, i, k), names) for k in reg["stores"] if k in L)
        board.append(f'<section class="tile {st}" data-region="{reg["id"]}"><div class="thead"><h3>{e(reg["title"])}</h3><span class="tsub">{e(reg["sub"])}</span>'
                     f'<span class="pill {st}">{STATUS_WORD[st]}</span></div><div class="tbody">{svg}<div class="ros">{ros}</div></div></section>')
        for cor in mp["corridors"]:
            if cor["after"] == reg["id"]:
                board.append(corridor_html(cor, day, cfg, mp))

    cards = []
    for reg in mp["regions"]:
        ks = [k for k in reg["stores"] if k in L]
        st = worst([L[k]["status"] for k in ks])
        evs = sorted({id(x): x for k in ks for x in store_events(r, i, k)}.values(),
                     key=lambda x: (-RANK[x["level"]], -SEVR[x["severity"]]))
        nflag = sum(1 for k in ks if L[k]["status"] == "flag")
        line = (f'<span class="rc-line">{e(evs[0]["title"])}, {e(evs[0]["when"])}</span>' if evs
                else '<span class="rc-line muted">No concerns this day</span>')
        count = f'<span class="rc-count">{nflag} of {len(ks)} stores flagged</span>' if nflag else ""
        cards.append((st, (f'<span class="pill {st}">{STATUS_WORD[st]}</span>', line + count)))
    ovw = overview_html(mp, cfg, xy, day, lambda k: L[k]["status"],
                        lambda k: f'{L[k]["name"]}: {STATUS_WORD[L[k]["status"]].lower()}', cards)

    return (f'<section class="view v{i}"><div class="grid"><div class="col">'
            f'<div class="board" data-zoom="all">{ovw}{zoombar(mp)}{"".join(board)}</div>{details_table_flag(day, cfg)}</div>'
            f'<aside class="panel"><div class="sum"><h2>{e(day["label"])}</h2>{flag_list}<h3>Truck runs</h3>{runs_html}'
            f'<p class="hint">Select a store, route or checkpoint for details.</p></div>{"".join(dets)}</aside></div></section>')


def outlook_view(j, o, cfg, xy, mp):
    L = o["locations"]
    names = {k: v["name"] for k, v in cfg["locations"].items()}
    groups = []
    for lv in ("a", "y", "g"):
        ks = [k for k in cfg["locations"] if k in L and L[k]["level"] == lv]
        if not ks:
            continue
        if lv == "g":
            body = f'<span>{e(", ".join(names[k] for k in ks))}</span>'
        else:
            body = "".join(f'<span><b>{e(names[k])}:</b> {e(L[k]["note"])}</span>' for k in ks)
        groups.append(f'<div class="ogroup"><b class="gh"><i class="osw o-{lv}"></i>{OUT_WORD[lv]}</b>{body}</div>')

    odets, rows, board = [], [], []
    for k in cfg["locations"]:
        if k not in L:
            continue
        v = L[k]
        hi = "–" if v["high"] is None else f'{v["high"]}°'
        lo = "–" if v["low"] is None else f'{v["low"]}°'
        odets.append(f'<div class="det" data-key="k:{k}"><button class="back" type="button">Back to the day</button>'
                     f'<h2>{e(names[k])}</h2><p><span class="lv {v["level"]}">{OUT_WORD[v["level"]]}</span> {e(v["note"])}</p>'
                     f'<dl class="facts"><dt>Conditions</dt><dd>{e(v["conditions"])}</dd><dt>High / low</dt><dd>{hi} / {lo}</dd><dt>Precip chance</dt><dd>{v["pop"]}%</dd></dl>'
                     f'<h3>Traffic cameras</h3><p class="lk">{links_html(cfg, k)}</p></div>')
        rows.append(f'<tr data-k="{k}"><td><span class="dotk o-{v["level"]}"></span>{e(names[k])}</td><td>{e(v["conditions"])}</td>'
                    f'<td class="n">{hi} / {lo}</td><td class="n">{v["pop"]}%</td><td>{e(v["note"])}</td><td class="lk">{links_html(cfg, k)}</td></tr>')
    for reg in mp["regions"]:
        tile = Tile(reg, xy)
        lv = max((L[k]["level"] for k in reg["stores"] if k in L), key={"g": 0, "y": 1, "a": 2}.get, default="g")
        svg = tile_svg(tile, reg, cfg, xy, None, names, lambda k: "o-" + L[k]["level"] if k in L else "o-g",
                       lambda k: f'{names[k]}: {OUT_WORD[L[k]["level"]].lower()}' if k in L else names[k], lanes=False)
        ros = "".join(f'<div class="ro o-{L[k]["level"]}" data-k="{k}" tabindex="0" role="button"><span class="ro-n">{e(names[k])}</span>'
                      f'<span class="ro-t">{"–" if L[k]["high"] is None else str(L[k]["high"]) + "°"} / {"–" if L[k]["low"] is None else str(L[k]["low"]) + "°"}</span>'
                      f'<span class="ro-c">{e(L[k]["note"] if L[k]["level"] != "g" else L[k]["conditions"])}</span></div>'
                      for k in reg["stores"] if k in L)
        board.append(f'<section class="tile o-{lv}" data-region="{reg["id"]}"><div class="thead"><h3>{e(reg["title"])}</h3><span class="tsub">{e(reg["sub"])}</span>'
                     f'<span class="lv {lv}">{OUT_WORD[lv]}</span></div><div class="tbody">{svg}<div class="ros">{ros}</div></div></section>')
    cards = []
    for reg in mp["regions"]:
        ks = [k for k in reg["stores"] if k in L]
        lv = max((L[k]["level"] for k in ks), key={"g": 0, "y": 1, "a": 2}.get, default="g")
        hot = [k for k in ks if L[k]["level"] == lv and lv != "g"]
        line = (f'<span class="rc-line">{e(names[hot[0]])}: {e(L[hot[0]]["note"])}</span>' if hot
                else '<span class="rc-line muted">Nothing notable</span>')
        cards.append((f"o-{lv}", (f'<span class="lv {lv}">{OUT_WORD[lv]}</span>', line)))
    ovw = overview_html(mp, cfg, xy, None, lambda k: "o-" + L[k]["level"] if k in L else "o-g",
                        lambda k: f'{names[k]}: {OUT_WORD[L[k]["level"]].lower()}' if k in L else names[k], cards)
    board.insert(0, ovw + zoombar(mp))
    board.append('<p class="onote">Early signals only. Truck routes and flags appear once a day is within 3 days.</p>')
    table = (f'<div class="tablewrap"><table><thead><tr><th>Location</th><th>Conditions</th><th class="n">High / Low</th><th class="n">Precip</th><th>Outlook</th><th>Traffic cameras</th></tr></thead>'
             f'<tbody>{"".join(rows)}</tbody></table></div>')
    return (f'<section class="view w{j}"><div class="grid"><div class="col"><div class="board" data-zoom="all">{"".join(board)}</div>{table}</div>'
            f'<aside class="panel"><div class="sum"><h2>{e(o["label"])}</h2><p><span class="conf">{e(o["confidence"])} confidence</span></p><p>{e(o["summary"])}</p>{"".join(groups)}'
            f'<p class="hint">Select a store for details.</p></div>{"".join(odets)}</aside></div></section>')


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------
CSS = """
:root{
--bg:#16212b;--grout:#0e161d;--tile:#1d2b37;--tile-2:#23344293;--raise:#26394a;
--text:#e3eaef;--muted:#91a3b0;--faint:#5f7483;--line:#2d4252;
--water:#13202a;--land:#1a2832;--ours:#22333f;--border:#3b5263;
--flag:#f0564a;--flag-soft:#3a2224;--watch:#f0a93a;--watch-soft:#3a3020;
--clear:#7cbd2c;--clear-soft:#1f3420;--idle:#5f7483;--brand:#65ab19;
--o-g:#9fcf7a;--o-y:#e9cf5a;--o-a:#f2a35c;--focus:#b8ff35;color-scheme:dark}
:root[data-theme="light"]{
--bg:#e9eef1;--grout:#c9d3da;--tile:#ffffff;--tile-2:#f4f7f9;--raise:#eef3f6;
--text:#15212b;--muted:#566874;--faint:#8a9aa6;--line:#d6dee4;
--water:#dce7ed;--land:#e9eef1;--ours:#ffffff;--border:#b6c4ce;
--flag:#c8322a;--flag-soft:#fbe5e1;--watch:#a8650a;--watch-soft:#fbf0d8;
--clear:#4d8723;--clear-soft:#e7f1dc;--idle:#8a9aa6;--brand:#4d8723;
--o-g:#9fcf7a;--o-y:#e2c13c;--o-a:#ef9a4c;--focus:#4d8723;color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 "Overpass","Segoe UI",Arial,sans-serif;font-variant-numeric:tabular-nums}
a{color:var(--clear)}
button{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.wrap{max-width:1340px;margin:0 auto;padding:18px 20px 40px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;gap:6px 20px;padding-bottom:12px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:12px;margin-right:auto}
.mark{display:grid;grid-template-columns:repeat(3,9px);gap:2px}
.mark i{width:9px;height:9px;background:var(--brand);border-radius:1px}.mark i:nth-child(2n){opacity:.55}.mark i:nth-child(3n){opacity:.8}
h1{margin:0;font-size:1.65rem;line-height:1;font-weight:800;letter-spacing:.01em}
.sub{color:var(--muted);margin:0;font-size:.9rem}
.theme{background:var(--tile);border:1px solid var(--line);border-radius:6px;padding:4px 12px;cursor:pointer;font-size:.85rem;color:var(--muted);display:none}
html.js .theme{display:inline-block}
.note{color:var(--flag);font-size:.9rem;margin:6px 0 0}
.bl{margin:16px 0 0;padding:14px 18px;background:var(--tile);border-left:6px solid var(--clear);border-radius:0 8px 8px 0;font-size:1.15rem;line-height:1.45;font-weight:600}
.bl.flag{border-color:var(--flag)}.bl.watch{border-color:var(--watch)}
.bl small{display:block;color:var(--muted);font-size:.78rem;font-weight:400;margin-top:6px}
input.r{position:absolute;opacity:0;pointer-events:none}
.rangebar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:18px 0 12px}
.rangebar>label{padding:8px 18px;font-weight:700;color:var(--text);background:var(--tile);border:1px solid var(--border);border-radius:8px;cursor:pointer;
box-shadow:0 1px 0 var(--grout)}
.rangebar>label:hover{border-color:var(--muted)}
.cnt{display:inline-block;background:var(--flag);color:#fff;border-radius:9px;font-size:.72rem;padding:0 7px;margin-left:6px;vertical-align:1px;box-shadow:0 0 0 2px var(--tile)}
.modes{margin-left:auto;display:flex}
.modes label{border:1px solid var(--border);background:var(--tile);padding:6px 16px;font-weight:700;font-size:.86rem;cursor:pointer;color:var(--muted)}
.modes label:first-child{border-radius:6px 0 0 6px}.modes label:last-child{border-radius:0 6px 6px 0;border-left:0}
.days{display:none;flex-wrap:wrap;gap:8px;margin:0 0 14px}
.days label{border:1px solid var(--line);background:var(--tile);border-radius:6px;padding:7px 14px;cursor:pointer;min-width:132px}
.days label b{display:block;font-size:1rem}.days label small{font-size:.8rem;color:var(--muted)}.days label small.f{color:var(--flag);font-weight:700}
.lv{display:inline-block;font-size:.75rem;font-weight:700;border-radius:4px;padding:1px 7px;color:#16212b;white-space:nowrap}
.lv.a{background:var(--o-a)}.lv.y{background:var(--o-y)}.lv.g{background:var(--o-g)}
.view{display:none}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 380px;gap:16px;align-items:start}
@media (max-width:980px){.grid{grid-template-columns:minmax(0,1fr)}}
.board{background:var(--grout);padding:6px;border-radius:8px;display:grid;gap:6px}
.tile{background:var(--tile);border-radius:4px;box-shadow:inset 0 1px 0 rgba(255,255,255,.04);border-top:3px solid var(--idle)}
.tile.flag{border-top-color:var(--flag)}.tile.watch{border-top-color:var(--watch)}.tile.clear{border-top-color:var(--clear)}
.tile.o-a{border-top-color:var(--o-a)}.tile.o-y{border-top-color:var(--o-y)}.tile.o-g{border-top-color:var(--o-g)}
.thead{display:flex;align-items:baseline;gap:10px;padding:10px 14px 0}
.thead h3{margin:0;font-size:1.2rem;font-weight:800}.tsub{color:var(--muted);font-size:.88rem;margin-right:auto}
.tbody{display:grid;grid-template-columns:minmax(0,1fr) 250px;gap:6px;align-items:start;padding:6px 8px 8px}
@media (max-width:720px){.tbody{grid-template-columns:minmax(0,1fr)}}

.tmap{display:block;width:100%;height:auto;background:var(--water);border-radius:3px}
.st{fill:var(--land);stroke:var(--border);stroke-width:1;vector-effect:non-scaling-stroke}.st-VA,.st-MD,.st-NC,.st-DC{fill:var(--ours)}
.stlbl{display:none}
.lane{fill:none;stroke-width:4;stroke-linecap:round;cursor:pointer}
.lane.clear{stroke:var(--clear);stroke-width:2.5;stroke-dasharray:1 7;opacity:.85}.lane.watch{stroke:var(--watch)}.lane.flag{stroke:var(--flag);stroke-width:5}
.lane.dim{opacity:.12}
.wp{cursor:pointer}.wp .hit{fill:transparent}
.wp .dia{fill:var(--tile);stroke-width:2.5}.wp.clear .dia{stroke:var(--clear)}.wp.watch .dia{stroke:var(--watch)}.wp.flag .dia{stroke:var(--flag);fill:var(--flag)}.wp.idle .dia{stroke:var(--idle)}
.wp:hover .dia,.wp.sel .dia{stroke:var(--text);stroke-width:3}
.exit{font-size:12px;font-weight:700;fill:var(--muted);paint-order:stroke;stroke:var(--water);stroke-width:4px}
.loc{cursor:pointer}.loc .dot{stroke:var(--tile);stroke-width:3}
.loc.clear .dot{fill:var(--clear)}.loc.watch .dot{fill:var(--watch)}.loc.flag .dot{fill:var(--flag)}
.loc.o-g .dot{fill:var(--o-g)}.loc.o-y .dot{fill:var(--o-y)}.loc.o-a .dot{fill:var(--o-a)}
.loc .ring{fill:none;stroke:var(--text);stroke-width:2;opacity:0}.loc:hover .ring{opacity:.45}.loc.sel .ring{opacity:1}
.loc text{font-size:15px;font-weight:700;fill:var(--text);paint-order:stroke;stroke:var(--water);stroke-width:5px;stroke-linejoin:round}
.loc.csc .hub{fill:var(--text)}.loc.csc text.tag{fill:var(--tile);stroke:none;font-size:10px;font-weight:800}
.ros{display:flex;flex-direction:column;gap:4px}
.ro{display:grid;grid-template-columns:1fr auto;gap:0 8px;padding:7px 10px;background:var(--raise);border-left:4px solid var(--clear);border-radius:0 4px 4px 0;cursor:pointer}
.ro.flag{border-color:var(--flag)}.ro.watch{border-color:var(--watch)}.ro.o-a{border-color:var(--o-a)}.ro.o-y{border-color:var(--o-y)}.ro.o-g{border-color:var(--o-g)}
.ro:hover,.ro.sel{background:var(--line)}
.ro-n{font-weight:700}.ro-t{color:var(--muted);font-size:.88rem}.ro-c{grid-column:1/-1;color:var(--muted);font-size:.84rem;line-height:1.35}
.ro.flag .ro-c{color:var(--text)}
.corridor{background:var(--tile-2);border-radius:4px;padding:10px 14px 12px;border-left:3px solid var(--idle)}
.corridor.flag{border-left-color:var(--flag)}.corridor.watch{border-left-color:var(--watch)}.corridor.clear{border-left-color:var(--clear)}
.chead{display:flex;align-items:center;gap:10px;margin-bottom:4px}.chead h3{margin:0;font-size:1rem;font-weight:800}
.crow{display:grid;grid-template-columns:auto 1fr auto;align-items:start;gap:0 10px;margin-top:8px;position:relative}
.cend{font-size:.82rem;font-weight:700;color:var(--muted);padding-top:3px;white-space:nowrap}
.clabel{position:absolute;left:0;top:-14px;font-size:.72rem;color:var(--faint)}
.track{display:flex;justify-content:space-around;position:relative;gap:8px}
.track::before{content:"";position:absolute;left:-6px;right:-6px;top:11px;height:2px;background:var(--line)}
.node{position:relative;display:flex;flex-direction:column;align-items:center;text-align:center;cursor:pointer;padding:0 4px;border-radius:4px;min-width:0}
.node i{width:12px;height:12px;transform:rotate(45deg);margin:5px 0 7px;border:2.5px solid var(--clear);background:var(--tile)}
.node.watch i{border-color:var(--watch)}.node.flag i{border-color:var(--flag);background:var(--flag)}.node.idle i{border-color:var(--idle)}
.node b{font-size:.88rem;line-height:1.2}.node span{font-size:.76rem;color:var(--muted);line-height:1.35}
.node:hover,.node.sel{background:var(--raise)}
.onote{margin:2px 6px;color:var(--muted);font-size:.85rem}
.overview,.zoombar{display:none}
html.js .overview{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:6px;background:var(--tile);border-radius:4px;padding:8px}
html.js .zoombar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;background:var(--tile);border-radius:4px;padding:8px}
html.js .board[data-zoom="all"] .tile,html.js .board[data-zoom="all"] .corridor,html.js .board[data-zoom="all"] .zoombar,html.js .board[data-zoom="all"] .onote{display:none}
html.js .board:not([data-zoom="all"]) .overview{display:none}
.ovmap{display:block;width:100%;height:auto;max-height:560px;background:var(--water);border-radius:3px}
.hot{cursor:pointer}.hot rect{fill:var(--clear);fill-opacity:.08;stroke:var(--clear);stroke-width:1.5;stroke-dasharray:5 4}
.hot.flag rect{fill:var(--flag);stroke:var(--flag)}.hot.watch rect{fill:var(--watch);stroke:var(--watch)}
.hot.o-a rect{fill:var(--o-a);stroke:var(--o-a)}.hot.o-y rect{fill:var(--o-y);stroke:var(--o-y)}.hot.o-g rect{fill:var(--o-g);stroke:var(--o-g)}
.hot:hover rect,.hot:focus-visible rect{fill-opacity:.2;stroke-dasharray:none;stroke-width:2}
.hot text{font-size:15px;font-weight:800;fill:var(--text);paint-order:stroke;stroke:var(--water);stroke-width:4px}
.ovmap .lane{stroke-width:3;pointer-events:none}.ovmap .lane.flag{stroke-width:4}.ovmap .lane.clear{stroke-width:2}
.ovmap .loc{pointer-events:none}.ovmap .loc .dot{stroke-width:2}
.ov-side{display:flex;flex-direction:column;gap:6px}
.rcard{display:flex;flex-direction:column;gap:2px;padding:12px 14px;background:var(--raise);border-left:5px solid var(--clear);border-radius:0 6px 6px 0;cursor:pointer}
.rcard.flag{border-color:var(--flag)}.rcard.watch{border-color:var(--watch)}.rcard.o-a{border-color:var(--o-a)}.rcard.o-y{border-color:var(--o-y)}.rcard.o-g{border-color:var(--o-g)}
.rcard:hover{background:var(--line)}
.rc-top{display:flex;align-items:center;justify-content:space-between;gap:8px}.rc-top b{font-size:1.15rem;font-weight:800}
.rc-sub{color:var(--muted);font-size:.84rem}.rc-line{font-size:.92rem;font-weight:600;margin-top:4px}.rc-line.muted{font-weight:400}
.rc-count{font-size:.82rem;color:var(--muted)}.rc-go{font-size:.82rem;font-weight:700;color:var(--clear);margin-top:4px}
.rc-go::after{content:" \\203A"}
.ov-hint{color:var(--muted);font-size:.84rem;margin:4px 2px 0}
.zoombar button{border:1px solid var(--border);background:var(--tile);color:var(--text);font-weight:700;font-size:.9rem;padding:6px 14px;border-radius:8px;cursor:pointer}
.zoombar .zout::before{content:"\\2039  "}
.zseg{display:flex;margin-left:auto}.zseg button{border-radius:0}.zseg button+button{border-left:0}
.zseg button:first-child{border-radius:8px 0 0 8px}.zseg button:last-child{border-radius:0 8px 8px 0}
.zseg button.on{background:var(--brand);border-color:var(--brand);color:#fff}
@media (prefers-reduced-motion:no-preference){html.js .board:not([data-zoom="all"]) .tile,html.js .board:not([data-zoom="all"]) .corridor{animation:zoomin .32s cubic-bezier(.2,.8,.3,1)}
html.js .board[data-zoom="all"] .overview{animation:zoomout .28s ease-out}}
@keyframes zoomin{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:none}}
@keyframes zoomout{from{opacity:0;transform:scale(1.03)}to{opacity:1;transform:none}}
.tablewrap{display:none;overflow-x:auto;background:var(--tile);border-radius:8px;border:1px solid var(--line)}
table{border-collapse:collapse;width:100%;min-width:600px;font-size:.92rem}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:700;font-size:.82rem}td.n,th.n{text-align:right;white-space:nowrap}td:first-child{white-space:nowrap;font-weight:700}
tr.flag td:first-child{box-shadow:inset 4px 0 0 var(--flag)}tr.watch td:first-child{box-shadow:inset 4px 0 0 var(--watch)}
td.lk a{margin-right:10px;white-space:nowrap}
.dotk{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;vertical-align:0}
.dotk.flag{background:var(--flag)}.dotk.watch{background:var(--watch)}.dotk.clear{background:var(--clear)}
.dotk.o-a{background:var(--o-a)}.dotk.o-y{background:var(--o-y)}.dotk.o-g{background:var(--o-g)}
.panel{background:var(--tile);border-radius:8px;padding:16px 18px;border:1px solid var(--line)}
@media (min-width:981px){.panel{position:sticky;top:12px;max-height:calc(100vh - 24px);overflow:auto}}
.panel h2{font-size:1.3rem;margin:0 0 8px;font-weight:800}.panel h3{font-size:.95rem;color:var(--muted);margin:16px 0 6px}
.conf{font-size:.78rem;font-weight:700;border:1px solid var(--line);border-radius:4px;padding:1px 7px;color:var(--muted)}
.flaglist{list-style:none;margin:0;padding:0}
.flaglist li{border-left:4px solid var(--flag);background:var(--flag-soft);padding:8px 11px;margin:0 0 8px;border-radius:0 6px 6px 0}
.flaglist li.watch{border-color:var(--watch);background:var(--watch-soft)}.flaglist li b{display:block;font-weight:700}
.flaglist small{display:block;color:var(--muted);font-size:.84rem;line-height:1.4}.flaglist small.when{color:var(--text)}
.tags{margin-bottom:3px;display:flex;align-items:center}.tags .ics{margin-left:auto}.ro-c .ics{margin:0 4px 0 0}
.sev,.chg{display:inline-block;font-size:.7rem;font-weight:800;padding:1px 7px;margin-right:6px;border-radius:3px}
.sev.significant{background:var(--flag);color:#fff}.sev.minor{background:transparent;color:var(--flag);box-shadow:inset 0 0 0 1.5px var(--flag)}
.sev.hu{background:transparent;color:var(--watch);box-shadow:inset 0 0 0 1.5px var(--watch)}
.chg{background:var(--text);color:var(--bg)}.chg.worse{background:var(--flag);color:#fff}.chg.better{background:var(--clear);color:#16212b}
.empty,.muted{color:var(--muted)}.small{font-size:.85rem}
.pill{display:inline-block;font-size:.74rem;font-weight:800;border-radius:4px;padding:1px 8px;white-space:nowrap}
.pill.flag{background:var(--flag);color:#fff}.pill.watch{background:var(--watch);color:#16212b}.pill.clear{background:var(--clear-soft);color:var(--clear)}
.pill.idle{background:transparent;color:var(--idle);box-shadow:inset 0 0 0 1px var(--idle)}
.runrow{display:grid;grid-template-columns:76px 1fr auto;align-items:center;gap:8px;padding:7px 4px;border-top:1px solid var(--line);cursor:pointer;border-radius:4px}
.runrow .pill{text-align:center}.runrow:hover{background:var(--raise)}.runrow .muted{font-size:.84rem}
.route{list-style:none;margin:6px 0 0;padding:0}
.route li{padding:5px 0 5px 12px;font-size:.9rem;border-left:2px solid var(--line);position:relative}
.route li.flag{border-left-color:var(--flag)}.route li.watch{border-left-color:var(--watch)}
.route li[data-k]{cursor:pointer}.route li small{display:block;color:var(--muted);font-size:.8rem;padding-left:2px}
.route .pill{min-width:70px;text-align:center;margin-right:6px}
.ic{width:18px;height:18px;flex:none;vertical-align:-4px;color:var(--muted)}
.ic.ice,.ic.alert{color:var(--flag)}.ic.snow{color:#3d8fd6}.ic.wind,.ic.cold{color:var(--watch)}.ic.ok{color:var(--clear)}
:root:not([data-theme="light"]) .ic.snow{color:#7db9ef}
.ics{display:inline-flex;gap:3px;margin-left:7px;vertical-align:-2px}.ics .ic{width:16px;height:16px}
.tl{list-style:none;margin:10px 0 0;padding:0;position:relative}
.tl-row{display:grid;grid-template-columns:66px minmax(0,1fr);gap:12px;padding:0 0 14px;position:relative}
.tl-row::before{content:"";position:absolute;left:77px;top:6px;bottom:-6px;width:3px;background:var(--clear);border-radius:2px}
.tl-row.watch::before{background:var(--watch)}.tl-row.flag::before{background:var(--flag)}.tl-row.idle::before{background:var(--idle)}
.tl-row:last-child::before{bottom:8px}
.tl-row[data-k]{cursor:pointer}
.tl-time{text-align:right;line-height:1.25;padding-top:1px}.tl-time b{display:block;font-size:.92rem}.tl-time span{font-size:.76rem;color:var(--muted)}
.tl-body{padding-left:14px;min-width:0}
.tl-head{display:flex;align-items:center;gap:6px;margin-bottom:2px}.tl-head .pill{min-width:70px;text-align:center}
.tl-where{display:block;font-weight:700;line-height:1.3}
.tl-why{display:block;font-size:.84rem;color:var(--muted);line-height:1.4}
.tl details{margin-top:3px}.tl summary{font-size:.8rem;color:var(--muted);cursor:pointer;width:max-content}
.tl summary:hover{color:var(--text)}
.tl-stops{list-style:none;margin:4px 0 0;padding:0}
.tl-stops li{display:flex;justify-content:space-between;gap:10px;font-size:.84rem;padding:3px 6px;border-radius:4px;cursor:pointer}
.tl-stops li span{color:var(--muted);white-space:nowrap}.tl-stops li:hover,.tl-row[data-k]:hover .tl-where{background:var(--raise)}
.ogroup{margin:12px 0 0}.ogroup span{display:block;font-size:.9rem;color:var(--muted);margin-top:2px}.ogroup span b{color:var(--text)}
.gh{display:block}.osw{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:7px;vertical-align:-1px}
.osw.o-a{background:var(--o-a)}.osw.o-y{background:var(--o-y)}.osw.o-g{background:var(--o-g)}
.facts{display:grid;grid-template-columns:auto 1fr;gap:3px 16px;margin:10px 0 0}.facts dt{color:var(--muted)}.facts dd{margin:0;font-weight:600}
.det,.hint{display:none}html.js .hint{display:block;font-size:.84rem;color:var(--muted);margin:14px 0 0}
.panel.open .sum{display:none}.panel.open .det.on{display:block}
.back{all:unset;cursor:pointer;color:var(--muted);font-size:.86rem;margin-bottom:8px;display:inline-block}.back:hover{color:var(--text)}
.back::before{content:"\\2039  "}
footer{color:var(--faint);font-size:.8rem;margin-top:18px}
@media (prefers-reduced-motion:no-preference){.lane.flag{animation:pulse 2.4s ease-in-out infinite}}
@keyframes pulse{50%{opacity:.55}}
@media (max-width:600px){.wrap{padding:14px 16px 32px}.crow{grid-template-columns:minmax(0,1fr)}.cend{display:none}
.track{flex-direction:column;gap:6px}.track::before{left:11px;right:auto;top:0;bottom:0;width:2px;height:auto}
.node{flex-direction:row;flex-wrap:wrap;align-items:center;text-align:left;gap:0 8px;padding:2px 4px}.node i{margin:0 6px 0 4px;flex:none}
.node span{flex-basis:100%;padding-left:30px}.clabel{position:static;display:block;margin-top:6px}.thead{flex-wrap:wrap}
.days label{min-width:0;flex:1}.bl{font-size:1.02rem}h1{font-size:1.35rem}
html.js .overview{grid-template-columns:minmax(0,1fr)}.zseg{margin-left:0}.rangebar>label{padding:7px 12px}}
"""

SCRIPT = """<script>
(function () {
  var root = document.documentElement;
  root.classList.add("js");
  try { var saved = localStorage.getItem("mws-theme"); if (saved) root.setAttribute("data-theme", saved); } catch (err) {}
  var tb = document.querySelector(".theme");
  function label() { tb.textContent = root.getAttribute("data-theme") === "light" ? "Dark mode" : "Light mode"; }
  if (tb) {
    label();
    tb.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("mws-theme", next); } catch (err) {}
      label();
    });
  }
  var zoom = "all";
  function setZoom(z) {
    zoom = z;
    document.querySelectorAll(".board").forEach(function (b) { b.setAttribute("data-zoom", z); });
    document.querySelectorAll(".zseg button").forEach(function (b) { b.classList.toggle("on", b.getAttribute("data-zoom") === z); });
  }
  function regionOf(v, id) {
    var t = v.querySelector('.tile [data-k="' + id + '"]');
    if (t) return t.closest(".tile").getAttribute("data-region");
    var c = v.querySelector('.corridor [data-k="' + id + '"]');
    return c ? c.closest(".corridor").getAttribute("data-regions").split(" ")[0] : null;
  }
  document.querySelectorAll(".view").forEach(function (v) {
    var panel = v.querySelector(".panel");
    function reset() {
      panel.classList.remove("open");
      v.querySelectorAll(".det.on").forEach(function (d) { d.classList.remove("on"); });
      v.querySelectorAll(".sel").forEach(function (x) { x.classList.remove("sel"); });
      v.querySelectorAll(".dim").forEach(function (x) { x.classList.remove("dim"); });
    }
    function show(key) {
      var d = v.querySelector('.det[data-key="' + key + '"]');
      if (!d) return;
      reset();
      panel.classList.add("open"); d.classList.add("on"); panel.scrollTop = 0;
      var id = key.slice(2);
      if (key[0] === "k") {
        var reg = regionOf(v, id);
        if (reg && reg !== zoom) setZoom(reg);
        v.querySelectorAll('[data-k="' + id + '"]').forEach(function (x) { if (!x.closest(".panel")) x.classList.add("sel"); });
      } else {
        v.querySelectorAll(".lane").forEach(function (x) { if (x.getAttribute("data-run") !== id) x.classList.add("dim"); });
      }
      if (window.innerWidth < 980) panel.scrollIntoView({behavior: "smooth", block: "start"});
    }
    function pick(t) {
      var k = t.getAttribute("data-k");
      show(k ? "k:" + k : "r:" + t.getAttribute("data-run"));
    }
    v.addEventListener("click", function (ev) {
      if (ev.target.closest(".back")) { reset(); return; }
      if (ev.target.closest("a")) return;
      var zb = ev.target.closest("[data-zoom]");
      if (zb && !zb.classList.contains("board")) {
        if (zb.getAttribute("data-zoom") === "all") reset();
        setZoom(zb.getAttribute("data-zoom"));
        var top = v.querySelector(".board").getBoundingClientRect().top;
        if (top < 0) window.scrollBy({top: top - 12, behavior: "smooth"});
        return;
      }
      var t = ev.target.closest("[data-k],[data-run]");
      if (t) pick(t);
    });
    v.addEventListener("keydown", function (ev) {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      var t = ev.target.closest('[role="button"]');
      if (!t) return;
      ev.preventDefault();
      if (t.hasAttribute("data-zoom")) { setZoom(t.getAttribute("data-zoom")); return; }
      pick(t);
    });
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    var open = document.querySelectorAll(".panel.open .back");
    if (open.length) open.forEach(function (b) { b.click(); });
    else if (zoom !== "all") setZoom("all");
  });
})();
</script>
"""


def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    r = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    basemap = BASEMAP_PATH.read_text(encoding="utf-8")
    gen = datetime.fromisoformat(r["generated"])
    mp = {**DEFAULT_MAP, **(cfg.get("map") or {})}

    xy = {k: project(v["lat"], v["lon"]) for k, v in cfg["locations"].items()}
    xy.update({k: project(v["lat"], v["lon"]) for k, v in cfg.get("checkpoints", {}).items()})

    for day in r["flag_days"]:
        for run in day["runs"]:
            t = datetime.strptime(run["depart"], "%H:%M")
            run["depart_label"] = t.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")

    fd, od = r["flag_days"], r["outlook_days"]
    radios = ('<input class="r" type="radio" name="range" id="r3" checked><input class="r" type="radio" name="range" id="r7">'
              + "".join(f'<input class="r" type="radio" name="fday" id="d{i}"{" checked" if i == 0 else ""}>' for i in range(len(fd)))
              + "".join(f'<input class="r" type="radio" name="oday" id="o{j}"{" checked" if j == 0 else ""}>' for j in range(len(od)))
              + '<input class="r" type="radio" name="mode" id="vmap" checked><input class="r" type="radio" name="mode" id="vdet">')

    def flag_label(i, d):
        nf = sum(1 for x in r.get("events", []) if x["level"] == "flag" and i in x.get("days", [])) + \
            sum(1 for x in r.get("run_events", []) if x["level"] == "flag" and x["day"] == i)
        nw = sum(1 for x in r.get("events", []) if x["level"] != "flag" and i in x.get("days", [])) + \
            sum(1 for x in r.get("run_events", []) if x["level"] != "flag" and x["day"] == i)
        txt = (f'<small class="f">{nf} flag{"s" if nf != 1 else ""}</small>' if nf
               else f'<small>{nw} heads-up{"s" if nw != 1 else ""}</small>' if nw else "<small>No flags</small>")
        return f'<label for="d{i}"><b>{e(d["label"])}</b>{txt}</label>'

    def out_label(j, o):
        return f'<label for="o{j}"><b>{e(o["label"])}</b><span class="lv {o["level"]}">{OUT_WORD[o["level"]]}</span></label>'

    tot = sum(1 for x in r.get("events", []) + r.get("run_events", []) if x["level"] == "flag")
    top = max((o["level"] for o in od), key={"g": 0, "y": 1, "a": 2}.get, default="g")
    tabs = (f'<div class="rangebar"><label for="r3">Next 3 days{f" <span class=cnt>{tot}</span>" if tot else ""}</label>'
            + (f'<label for="r7">Days 4 to 7{"" if top == "g" else f" <span class=\"lv {top}\">{OUT_WORD[top]}</span>"}</label>' if od else "")
            + '<span class="modes"><label for="vmap">Map</label><label for="vdet">Details</label></span></div>')
    bar = (tabs + f'<div class="days bar3">{"".join(flag_label(i, d) for i, d in enumerate(fd))}</div>'
           + f'<div class="days bar7">{"".join(out_label(j, o) for j, o in enumerate(od))}</div>')
    views = "".join(flag_view(i, d, cfg, xy, r, mp) for i, d in enumerate(fd))
    views += "".join(outlook_view(j, o, cfg, xy, mp) for j, o in enumerate(od))

    sel = "border-color:var(--text);box-shadow:inset 0 0 0 1px var(--text)"
    show = ("#r3:checked~.bar3,#r7:checked~.bar7{display:flex}"
            "#r3:checked~.rangebar label[for=r3],#r7:checked~.rangebar label[for=r7]{background:var(--brand);border-color:var(--brand);color:#fff}"
            "#vmap:checked~.rangebar label[for=vmap],#vdet:checked~.rangebar label[for=vdet]{background:var(--raise);color:var(--text)}"
            "#vdet:checked~.views .board{display:none}#vdet:checked~.views .tablewrap{display:block}")
    for reg in mp["regions"]:
        rid = reg["id"]
        show += (f'html.js .board[data-zoom="{rid}"] .tile:not([data-region="{rid}"]),'
                 f'html.js .board[data-zoom="{rid}"] .corridor:not([data-regions~="{rid}"]){{display:none}}')
    for i in range(len(fd)):
        show += f"#r3:checked~#d{i}:checked~.views .v{i}{{display:block}}#d{i}:checked~.bar3 label[for=d{i}]{{{sel}}}"
    for j in range(len(od)):
        show += f"#r7:checked~#o{j}:checked~.views .w{j}{{display:block}}#o{j}:checked~.bar7 label[for=o{j}]{{{sel}}}"
    show += "".join(f"#{x}:focus-visible~* label[for={x}]{{outline:2px solid var(--focus);outline-offset:2px}}"
                    for x in ["r3", "r7", "vmap", "vdet"] + [f"d{i}" for i in range(len(fd))] + [f"o{j}" for j in range(len(od))])

    status = ("flag" if any(x["level"] == "flag" for x in r.get("events", []) + r.get("run_events", []))
              else "watch" if r.get("events") or r.get("run_events") else "clear")
    ai_note = '<small>Summary written by Claude from the forecast data.</small>' if r.get("bottom_line_by_ai") else ""
    note = f'<p class="note">No forecast available for: {e(", ".join(r["missing"]))}.</p>' if r.get("missing") else ""
    va = cfg.get("links", {}).get("sigalert_va_default", {})
    sig_va = sig_url(cfg, va.get("lat"), va.get("lon"), va.get("zoom")) if va else None
    sig_link = f' <a href="{e(sig_va)}" target="_blank" rel="noopener">SigAlert: Virginia</a>' if sig_va else ""

    html = f"""<!doctype html>
<html lang="en" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mosaic Weather System: {e(fd[0]['label'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Overpass:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>{CSS}{show}</style></head>
<body><svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="base" viewBox="0 0 900 640" width="900" height="640" overflow="visible">{basemap}</symbol>{ICON_DEFS}</defs></svg>
<div class="wrap">{radios}
<header><div class="brand"><span class="mark" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i><i></i></span>
<div><h1>Mosaic Weather System</h1><p class="sub">Forecast as of {gen.strftime('%I:%M %p').lstrip('0')}, {gen.strftime('%A, %b ')}{gen.day}.{sig_link}</p></div></div>
<button class="theme" type="button">Dark mode</button>{note}</header>
<div class="bl {status}">{e(r.get("bottom_line", ""))}{ai_note}</div>
{bar}
<div class="views">{views}</div>
<footer>Sources: National Weather Service, with Open-Meteo as backup. Map positions are approximate. Reporting only, not a go or no-go call.</footer>
</div>{SCRIPT}</body></html>"""
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Saved {OUT_PATH.relative_to(ROOT)} ({len(html) // 1024} KB)")


if __name__ == "__main__":
    main()
