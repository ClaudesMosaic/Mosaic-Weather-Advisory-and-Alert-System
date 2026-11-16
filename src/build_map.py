"""
Mosaic Weather Advisory and Alert System: weather map page

Reads config/network.yaml, output/results.json and assets/basemap.svg and
writes output/weather-map.html, a single self-contained page that gets
attached to the morning email.

It works without JavaScript (day switching is pure HTML and CSS), so it opens
cleanly in a browser, in Outlook's attachment preview and on a phone.

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
    return round(_TX + _K * r * math.sin(_N * x), 1), round(_TY - _K * (_F - r * math.cos(_N * x)), 1)


# Enlarged inset for the DC and Baltimore cluster
SRC = (500, 50, 112, 118)          # x, y, w, h on the main map
INS = (666, 160, 2)                # x, y, zoom
LABELS = {  # main-map label offsets: dx, dy, anchor
    "csc": (14, 5, "start"), "richmond": (-13, 5, "end"), "vabeach": (-4, 26, "middle"),
    "raleigh": (12, 5, "start"), "greensboro": (0, -14, "middle"),
}
INSET_LABELS = {
    "chantilly": (0, -15, "middle"), "fairfax": (4, 24, "end"), "alexandria": (12, 5, "start"),
    "nbethesda": (0, -15, "middle"), "owings": (-12, 5, "end"),
}
CAMERA_NAMES = {"VA": "VDOT 511 cameras", "MD": "MD CHART cameras", "NC": "DriveNC cameras"}
STATUS_WORD = {"flag": "Flagged", "watch": "Heads-up", "clear": "Clear"}
OUT_WORD = {"a": "Potential hazard", "y": "Worth watching", "g": "Nothing notable"}
RANK = {"clear": 0, "watch": 1, "flag": 2}


def e(s):
    return escape(str(s)) if s is not None else ""


def in_inset(x, y):
    sx, sy, sw, sh = SRC
    return sx <= x <= sx + sw and sy <= y <= sy + sh


def to_inset(x, y):
    sx, sy, _, _ = SRC
    ix, iy, z = INS
    return round(ix + (x - sx) * z, 1), round(iy + (y - sy) * z, 1)


def sig_url(cfg, lat, lon, zoom):
    tpl = cfg.get("links", {}).get("sigalert")
    return tpl.format(lat=lat, lon=lon, zoom=zoom) if tpl else None


# ----------------------------------------------------------------------
# SVG pieces
# ----------------------------------------------------------------------
def lanes_svg(day, xy):
    """Truck runs drawn stretch by stretch, worst status on top."""
    segs = []
    for run in day["runs"]:
        pts = run["points"]
        for a, b in zip(pts, pts[1:]):
            if a["id"] not in xy or b["id"] not in xy:
                continue
            s = a["status"] if RANK[a["status"]] >= RANK[b["status"]] else b["status"]
            segs.append((RANK[s], s, xy[a["id"]], xy[b["id"]], f"{run['name']}: {a['name']} to {b['name']}, {STATUS_WORD[s].lower()}"))
    segs.sort(key=lambda t: t[0])
    return "".join(f'<path class="lane {s}" d="M{p[0]},{p[1]} L{q[0]},{q[1]}"><title>{e(t)}</title></path>'
                   for _, s, p, q, t in segs)


def checkpoint_dots(day, cfg, xy, scale=1):
    worst = {}
    for run in day["runs"]:
        for p in run["points"]:
            if p["id"] in cfg.get("checkpoints", {}):
                cur = worst.get(p["id"], "clear")
                worst[p["id"]] = p["status"] if RANK[p["status"]] > RANK[cur] else cur
    r = 4 if scale == 1 else 3
    return "".join(f'<circle class="cp {s}" cx="{xy[k][0]}" cy="{xy[k][1]}" r="{r}"><title>{e(cfg["checkpoints"][k]["name"])}: {STATUS_WORD[s].lower()}</title></circle>'
                   for k, s in worst.items())


def loc_marks(cfg, xy, cls_of, tip_of, inset):
    out = []
    for k, loc in cfg["locations"].items():
        x, y = xy[k]
        cls, tip = cls_of(k), tip_of(k)
        if inset:
            if not in_inset(*project(loc["lat"], loc["lon"])) or k not in INSET_LABELS:
                continue
            dx, dy, anc = INSET_LABELS[k]
            out.append(f'<g class="loc {cls}"><title>{e(tip)}</title><circle class="dot" cx="{x}" cy="{y}" r="8"/>'
                       f'<text x="{x + dx}" y="{y + dy}" text-anchor="{anc}">{e(loc["name"])}</text></g>')
            continue
        small = k in INSET_LABELS
        if k == "csc":
            out.append(f'<g class="loc {cls} csc"><title>{e(tip)}</title><rect x="{x - 12}" y="{y - 12}" width="24" height="24" rx="4"/>'
                       f'<circle class="dot" cx="{x + 12}" cy="{y - 12}" r="7"/><text class="tag" x="{x}" y="{y + 4}" text-anchor="middle">CSC</text>'
                       f'<text x="{x + 14}" y="{y + 5}">Doswell</text></g>')
        elif small:
            out.append(f'<g class="loc {cls}"><title>{e(tip)}</title><circle class="dot" cx="{x}" cy="{y}" r="5"/></g>')
        else:
            dx, dy, anc = LABELS.get(k, (12, 5, "start"))
            out.append(f'<g class="loc {cls}"><title>{e(tip)}</title><circle class="dot" cx="{x}" cy="{y}" r="8"/>'
                       f'<text x="{x + dx}" y="{y + dy}" text-anchor="{anc}">{e(loc["name"])}</text></g>')
    return "".join(out)


def map_svg(cfg, inner_main, inner_inset_scaled, inner_inset_marks, label):
    ix, iy, _ = INS
    sx, sy, sw, sh = SRC
    return (f'<svg viewBox="0 0 900 640" role="img" aria-label="{e(label)}">'
            f'<use href="#base" width="900" height="640"/>'
            f'<rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" class="srcbox"/>'
            f'{inner_main}'
            f'<g clip-path="url(#insclip)"><rect x="{ix}" y="{iy}" width="224" height="236" class="insbg"/>'
            f'<g transform="translate({ix - sx * 2},{iy - sy * 2}) scale(2)"><use href="#base" width="900" height="640"/>{inner_inset_scaled}</g></g>'
            f'<rect x="{ix}" y="{iy}" width="224" height="236" rx="8" class="insframe"/>'
            f'{inner_inset_marks}'
            f'<text x="{ix + 10}" y="{iy + 252}" class="institle">DC and Baltimore area, enlarged</text></svg>')


# ----------------------------------------------------------------------
# Day views
# ----------------------------------------------------------------------
def links_html(cfg, k):
    loc = cfg["locations"][k]
    cams = cfg.get("links", {}).get("cameras", {})
    parts = []
    if cams.get(loc["state"]):
        parts.append(f'<a href="{e(cams[loc["state"]])}" target="_blank" rel="noopener">{CAMERA_NAMES.get(loc["state"], "Cameras")}</a>')
    sig = sig_url(cfg, loc["lat"], loc["lon"], cfg.get("links", {}).get("sigalert_store_zoom", 4))
    if sig:
        parts.append(f'<a href="{e(sig)}" target="_blank" rel="noopener">SigAlert</a>')
    return " ".join(parts)


def flag_view(i, day, cfg, xy, xy_in):
    L = day["locations"]
    cls_of = lambda k: L[k]["status"]  # noqa: E731
    tip_of = lambda k: f'{L[k]["name"]}: {STATUS_WORD[L[k]["status"]].lower()}'  # noqa: E731
    main = lanes_svg(day, xy) + checkpoint_dots(day, cfg, xy) + loc_marks(cfg, xy, cls_of, tip_of, False)
    inset_scaled = lanes_svg(day, xy).replace('class="lane', 'class="lane ns') + checkpoint_dots(day, cfg, xy, 2)
    inset_marks = loc_marks(cfg, xy_in, cls_of, tip_of, True)

    items = []
    for k, v in L.items():
        for f in v["flags"]:
            items.append((RANK[f["level"]], f'<li class="{f["level"]}"><b>{e(v["name"])}: {e(f["rule"])}</b><small>{e(f["when"])}. {e(f["text"])}</small></li>'))
    for run in day["runs"]:
        if run["status"] != "clear":
            bad = [p for p in run["points"] if p["status"] != "clear"]
            detail = "; ".join(f'{p["name"]}, {p["window"]}: {", ".join(p["reasons"]).lower()}' for p in bad)
            items.append((RANK[run["status"]], f'<li class="{run["status"]}"><b>{e(run["name"])} run</b><small>{e(detail)}.</small></li>'))
    items.sort(key=lambda t: -t[0])
    flag_list = (f'<ul class="flaglist">{"".join(h for _, h in items)}</ul>' if items
                 else '<p class="empty">No flags or heads-ups for this day.</p>')

    runs = []
    for run in day["runs"]:
        steps = "".join(f'<li><span class="pill {p["status"]}">{STATUS_WORD[p["status"]]}</span> {e(p["name"])} <span class="muted">{e(p["window"])}</span></li>'
                        for p in run["points"])
        runs.append(f'<details><summary><span class="pill {run["status"]}">{STATUS_WORD[run["status"]]}</span> '
                    f'<b>{e(run["name"])}</b> <span class="muted">{e(run["operator"])}, leaves {e(run["depart_label"])}</span></summary>'
                    f'<ul class="route">{steps}</ul></details>')
    runs_html = "".join(runs) or '<p class="empty">No truck runs scheduled this day.</p>'

    rows = []
    for k, v in L.items():
        s = v["summary"]
        hi = "–" if s["high"] is None else f'{s["high"]}°'
        lo = "–" if s["low"] is None else f'{s["low"]}°'
        rows.append(f'<tr class="{v["status"]}"><td><span class="dotk {v["status"]}"></span><b>{e(v["name"])}</b></td><td>{e(s["conditions"])}</td>'
                    f'<td class="n">{hi} / {lo}</td><td class="n">{s["pop"]}%</td><td class="n">{s["rain_in"]:.2f}"</td>'
                    f'<td class="n">{s["snow_in"]:.1f}"</td><td class="lk">{links_html(cfg, k)}</td></tr>')

    return (f'<section class="view v{i}"><div class="grid"><div class="mapbox">{map_svg(cfg, main, inset_scaled, inset_marks, "Truck lanes and locations for " + day["label"])}'
            f'<div class="legend"><span><i class="sw" style="background:var(--flag)"></i>Flagged</span><span><i class="sw" style="background:var(--watch)"></i>Heads-up</span>'
            f'<span><i class="sw" style="background:var(--clear)"></i>Clear</span><span>Lanes are colored stretch by stretch for the hours each truck is expected there. Hover for names.</span></div></div>'
            f'<aside class="panel"><h2>{e(day["label"])}</h2>{flag_list}<h3>Truck runs</h3>{runs_html}</aside></div>'
            f'<div class="tablewrap"><table><thead><tr><th>Location</th><th>Conditions</th><th class="n">High / Low</th><th class="n">Precip</th>'
            f'<th class="n">Rain</th><th class="n">Snow</th><th>Traffic</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>')


def outlook_view(i, o, cfg, xy, xy_in):
    L = o["locations"]
    names = {k: v["name"] for k, v in cfg["locations"].items()}
    cls_of = lambda k: "o-" + L[k]["level"] if k in L else "o-g"  # noqa: E731
    tip_of = lambda k: f'{names[k]}: {OUT_WORD[L[k]["level"]].lower()}' if k in L else names[k]  # noqa: E731
    main = loc_marks(cfg, xy, cls_of, tip_of, False)
    inset_marks = loc_marks(cfg, xy_in, cls_of, tip_of, True)

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

    rows = []
    for k in cfg["locations"]:
        if k not in L:
            continue
        v = L[k]
        hi = "–" if v["high"] is None else f'{v["high"]}°'
        lo = "–" if v["low"] is None else f'{v["low"]}°'
        rows.append(f'<tr><td><span class="dotk o-{v["level"]}"></span><b>{e(names[k])}</b></td><td>{e(v["conditions"])}</td>'
                    f'<td class="n">{hi} / {lo}</td><td class="n">{v["pop"]}%</td><td>{e(v["note"])}</td><td class="lk">{links_html(cfg, k)}</td></tr>')

    return (f'<section class="view v{i}"><div class="grid"><div class="mapbox">{map_svg(cfg, main, "", inset_marks, "Outlook for " + o["label"])}'
            f'<div class="legend"><span><i class="sw" style="background:var(--o-a)"></i>Potential hazard</span><span><i class="sw" style="background:var(--o-y)"></i>Worth watching</span>'
            f'<span><i class="sw" style="background:var(--o-g)"></i>Nothing notable</span><span>Early signals, not flags. Truck lanes appear once a day is within 3 days.</span></div></div>'
            f'<aside class="panel"><h2>{e(o["label"])} <span class="conf">{e(o["confidence"])} confidence</span></h2><p>{e(o["summary"])}</p>{"".join(groups)}</aside></div>'
            f'<div class="tablewrap"><table><thead><tr><th>Location</th><th>Conditions</th><th class="n">High / Low</th><th class="n">Precip</th><th>Outlook</th><th>Traffic</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></section>')


# ----------------------------------------------------------------------
# Page
# ----------------------------------------------------------------------
CSS = """
:root{--o-g:#c6e5a6;--o-y:#f4e07a;--o-a:#f6b46a;--bg:#eef1ec;--surface:#fff;--ink:#1c2620;--muted:#5b6a60;--line:#d3dbd2;
--land:#e2e8df;--land-line:#b9c4b6;--water:#d6e3e8;--green:#4d8723;--brand:#65ab19;--clear:#4d8723;--watch:#b77a0e;--flag:#c23b2a;
--flag-soft:#fbe5e1;--watch-soft:#fbf0d8;--clear-soft:#e7f1dc;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#141a16;--surface:#1d2520;--ink:#e7ede6;--muted:#a2b0a5;--line:#34403a;--land:#26302a;
--land-line:#465349;--water:#18242a;--clear:#8cc653;--watch:#e3ac45;--flag:#f07a66;--flag-soft:#3e2420;--watch-soft:#3a3020;--clear-soft:#243320;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 "Segoe UI",Arial,Helvetica,sans-serif}
a{color:var(--green)}@media (prefers-color-scheme:dark){a{color:var(--clear)}}
.wrap{max-width:1180px;margin:0 auto;padding:18px 18px 40px}
header{border-bottom:4px solid var(--brand);padding-bottom:10px}h1{margin:0;font-size:1.8rem;line-height:1.1}.sub{color:var(--muted);margin:4px 0 0;font-size:.9rem}
.note{color:var(--flag);font-size:.9rem}
input.r{position:absolute;opacity:0;pointer-events:none}
.bar{display:flex;flex-wrap:wrap;gap:6px 18px;margin:16px 0 12px;align-items:flex-end}
.grp{display:flex;flex-direction:column;gap:6px}.grp>span{font-size:.85rem;font-weight:600;color:var(--muted)}
.days{display:flex;flex-wrap:wrap;gap:6px}
.days label{border:2px solid var(--line);background:var(--surface);border-radius:8px;padding:6px 12px;cursor:pointer;min-width:118px}
.days label b{display:block;font-size:1rem}.days label small{font-size:.8rem;color:var(--muted)}
.days label small.f{color:var(--flag);font-weight:600}
.lv{display:inline-block;font-size:.75rem;font-weight:600;border-radius:4px;padding:0 6px;color:#1c2620}
.lv.a{background:var(--o-a)}.lv.y{background:var(--o-y)}.lv.g{background:var(--o-g)}
.view{display:none}
.grid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(280px,1fr);gap:14px;align-items:start}
@media (max-width:860px){.grid{grid-template-columns:1fr}}
.mapbox{background:var(--water);border:1px solid var(--line);border-radius:10px;overflow:hidden}
svg{display:block;width:100%;height:auto}
.st{fill:var(--land);stroke:var(--land-line);stroke-width:1.2}.st-VA,.st-MD,.st-NC,.st-DC{fill:var(--surface)}
.stlbl{font:600 22px "Segoe UI",Arial,sans-serif;fill:var(--land-line);letter-spacing:.2em;text-anchor:middle}
.lane{fill:none;stroke-width:4;stroke-linecap:round;stroke-linejoin:round;opacity:.9}
.lane.clear{stroke:var(--clear);stroke-dasharray:2 9;stroke-width:3;opacity:.7}.lane.watch{stroke:var(--watch)}.lane.flag{stroke:var(--flag);stroke-width:5}
.lane.ns{vector-effect:non-scaling-stroke}
.cp{stroke:var(--surface);stroke-width:1.5}.cp.clear{fill:var(--clear)}.cp.watch{fill:var(--watch)}.cp.flag{fill:var(--flag)}
.loc .dot{stroke:var(--surface);stroke-width:3}.loc.clear .dot{fill:var(--clear)}.loc.watch .dot{fill:var(--watch)}.loc.flag .dot{fill:var(--flag)}
.loc.o-g .dot{fill:var(--o-g)}.loc.o-y .dot{fill:var(--o-y)}.loc.o-a .dot{fill:var(--o-a)}
.loc[class*=" o-"] .dot{stroke:#5b6a60;stroke-width:1.5}
.loc text{font:600 15px "Segoe UI",Arial,sans-serif;fill:var(--ink);paint-order:stroke;stroke:var(--surface);stroke-width:4px;stroke-linejoin:round}
.csc rect{fill:var(--ink)}.csc text.tag{fill:var(--surface);stroke:none;font:700 11px Arial,sans-serif}
.srcbox{fill:none;stroke:var(--ink);stroke-width:1.5;stroke-dasharray:4 4;opacity:.6}.insbg{fill:var(--water)}
.insframe{fill:none;stroke:var(--ink);stroke-width:2}.institle{font:600 13px "Segoe UI",Arial,sans-serif;fill:var(--muted)}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;padding:8px 12px;font-size:.82rem;color:var(--muted);background:var(--surface);border-top:1px solid var(--line)}
.sw{display:inline-block;width:20px;height:4px;border-radius:2px;vertical-align:middle;margin-right:6px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.panel h2{font-size:1.3rem;margin:0 0 6px}.panel h3{font-size:1rem;color:var(--muted);margin:14px 0 6px}
.conf{font-size:.75rem;font-weight:600;border:1px solid var(--line);border-radius:4px;padding:0 6px;color:var(--muted);vertical-align:middle}
.flaglist{list-style:none;margin:0;padding:0}.flaglist li{border-left:4px solid var(--flag);background:var(--flag-soft);padding:7px 10px;margin:0 0 7px;border-radius:0 6px 6px 0}
.flaglist li.watch{border-color:var(--watch);background:var(--watch-soft)}.flaglist small{display:block;color:var(--muted)}
.empty,.muted{color:var(--muted)}
details{border-top:1px solid var(--line);padding:6px 0}summary{cursor:pointer}
.pill{display:inline-block;font-size:.75rem;font-weight:600;border-radius:4px;padding:0 7px;min-width:66px;text-align:center}
.pill.flag{background:var(--flag-soft);color:var(--flag)}.pill.watch{background:var(--watch-soft);color:var(--watch)}.pill.clear{background:var(--clear-soft);color:var(--clear)}
.route{list-style:none;margin:6px 0 0;padding:0 0 0 4px;border-left:3px solid var(--line)}.route li{padding:2px 0 2px 10px;font-size:.88rem}
.ogroup{margin:10px 0 0}.ogroup span{display:block;font-size:.9rem;color:var(--muted)}.ogroup span b{color:var(--ink)}
.gh{display:block}.osw{display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:-1px;margin-right:6px;border:1px solid #5b6a60}
.osw.o-a{background:var(--o-a)}.osw.o-y{background:var(--o-y)}.osw.o-g{background:var(--o-g)}
.tablewrap{overflow-x:auto;margin-top:14px;background:var(--surface);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;min-width:720px;font-size:.9rem}th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--green);color:#fff;font-weight:600}td.n,th.n{text-align:right;white-space:nowrap}
tr.flag td{background:var(--flag-soft)}tr.watch td{background:var(--watch-soft)}td.lk a{margin-right:10px;white-space:nowrap}
.dotk{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px}
.dotk.flag{background:var(--flag)}.dotk.watch{background:var(--watch)}.dotk.clear{background:var(--clear)}
.dotk.o-a{background:var(--o-a)}.dotk.o-y{background:var(--o-y)}.dotk.o-g{background:var(--o-g)}
footer{color:var(--muted);font-size:.8rem;margin-top:16px}
"""


def main():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    r = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    basemap = BASEMAP_PATH.read_text(encoding="utf-8")
    gen = datetime.fromisoformat(r["generated"])

    xy = {k: project(v["lat"], v["lon"]) for k, v in cfg["locations"].items()}
    xy.update({k: project(v["lat"], v["lon"]) for k, v in cfg.get("checkpoints", {}).items()})
    xy_in = {k: to_inset(*p) for k, p in xy.items()}

    for day in r["flag_days"]:
        for run in day["runs"]:
            t = datetime.strptime(run["depart"], "%H:%M")
            run["depart_label"] = t.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")

    days = r["flag_days"] + r["outlook_days"]
    radios = "".join(f'<input class="r" type="radio" name="day" id="d{i}"{" checked" if i == 0 else ""}>' for i in range(len(days)))

    def flag_label(i, d):
        nf = sum(1 for v in d["locations"].values() if v["status"] == "flag") + sum(1 for x in d["runs"] if x["status"] == "flag")
        txt = f'<small class="f">{nf} flag{"s" if nf != 1 else ""}</small>' if nf else "<small>No flags</small>"
        return f'<label for="d{i}"><b>{e(d["label"])}</b>{txt}</label>'

    def out_label(i, o):
        return f'<label for="d{i}"><b>{e(o["label"])}</b><span class="lv {o["level"]}">{OUT_WORD[o["level"]]}</span></label>'

    nflag = len(r["flag_days"])
    bar = (f'<div class="bar"><div class="grp"><span>Next 3 days</span><div class="days">{"".join(flag_label(i, d) for i, d in enumerate(r["flag_days"]))}</div></div>'
           + (f'<div class="grp"><span>What\'s coming, days 4 to 7</span><div class="days">{"".join(out_label(nflag + j, o) for j, o in enumerate(r["outlook_days"]))}</div></div>' if r["outlook_days"] else "")
           + "</div>")
    views = "".join(flag_view(i, d, cfg, xy, xy_in) for i, d in enumerate(r["flag_days"]))
    views += "".join(outlook_view(nflag + j, o, cfg, xy, xy_in) for j, o in enumerate(r["outlook_days"]))
    show = "".join(f'#d{i}:checked~.views .v{i}{{display:block}}#d{i}:checked~.bar label[for=d{i}]{{border-color:var(--green);box-shadow:inset 0 0 0 1px var(--green)}}'
                   f'#d{i}:focus-visible~.bar label[for=d{i}]{{outline:3px solid var(--brand);outline-offset:2px}}' for i in range(len(days)))

    note = f'<p class="note">No forecast available for: {e(", ".join(r["missing"]))}.</p>' if r.get("missing") else ""
    va = cfg.get("links", {}).get("sigalert_va_default", {})
    sig_va = sig_url(cfg, va.get("lat"), va.get("lon"), va.get("zoom")) if va else None
    sig_link = f' <a href="{e(sig_va)}" target="_blank" rel="noopener">SigAlert: Virginia</a>' if sig_va else ""

    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mosaic Weather System: {e(r['flag_days'][0]['label'])}</title><style>{CSS}{show}</style></head>
<body><svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>
<symbol id="base" viewBox="0 0 900 640" width="900" height="640" overflow="visible">{basemap}</symbol>
<clipPath id="insclip"><rect x="{INS[0]}" y="{INS[1]}" width="224" height="236" rx="8"/></clipPath></defs></svg>
<div class="wrap">{radios}
<header><h1>Mosaic Weather System</h1><p class="sub">Weather map for {gen.strftime('%A, %b ')}{gen.day}, generated {gen.strftime('%I:%M %p').lstrip('0')}.{sig_link}</p>{note}</header>
{bar}
<div class="views">{views}</div>
<footer>Sources: National Weather Service, with Open-Meteo as backup. Map positions are approximate. Reporting only, not a go or no-go call.</footer>
</div></body></html>"""
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"Saved {OUT_PATH.relative_to(ROOT)} ({len(html) // 1024} KB)")


if __name__ == "__main__":
    main()
