"""
metro_orientation.py
Orientation of metro lines in Indian cities, after Boeing (2019),
"Urban spatial order: street network orientation, configuration, and entropy".

For each city:
  1. Pull metro route relations (with member-way geometry) from Overpass.
  2. Compute the bearing and geodesic length of every track segment.
  3. Build a 36-bin, length-weighted, bidirectional bearing histogram.
  4. Report orientation entropy H and orientation order
         phi = 1 - ((H - H_grid) / (H_max - H_grid))**2
     with H_grid = ln 4 and H_max = ln 36.
  5. Report a small-sample null: phi for the same segment lengths with
     random bearings. Small or young networks score "ordered" by chance,
     and this shows how much.
  6. Because phi is rotation-invariant (and cannot tell a grid from a single
     tilted corridor), also report the main axis bearing, its tilt off
     N-S/E-W, the share of track on the main axis vs its perpendicular, and
     two axial moments: R2 (one-axis concentration) and R4 (grid or axis).
  7. Draw a polar rose (bar AREA = share of length, stacked by line colour)
     with a small inset map, plus a comparison grid of all cities.

Default counts each line once (the longest direction relation per line), so
parallel up/down tracks are not double-counted. Use --all-tracks to count
every unique way instead, which is closer to the original Reddit post.

Usage:
    pip install requests pyproj numpy matplotlib
    python metro_orientation.py                    # all cities
    python metro_orientation.py delhi kolkata      # some cities
    python metro_orientation.py --all-tracks
    python metro_orientation.py --refresh          # ignore cached Overpass data

Outputs go to ./out/ (one PNG per city, grid.png, summary.csv).
Data (c) OpenStreetMap contributors, ODbL.
"""

import argparse
import csv
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import requests
from pyproj import Geod

# --------------------------------------------------------------------------
# City definitions: bbox is (south, west, north, east); network is a regex
# matched case-insensitively against the relation's network/operator/name.
# Adjust if OSM tagging changes or you want NCR extensions excluded.
# --------------------------------------------------------------------------
CITIES = {
    "delhi":     {"label": "Delhi",     "bbox": (28.30, 76.80, 28.90, 77.60),
                  "network": r"delhi metro|dmrc"},
    "kolkata":   {"label": "Kolkata",   "bbox": (22.40, 88.20, 22.75, 88.55),
                  "network": r"kolkata metro"},
    "mumbai":    {"label": "Mumbai",    "bbox": (18.85, 72.75, 19.35, 73.10),
                  "network": r"mumbai metro",
                  # Navi Mumbai Metro is a separate CIDCO system; Line 11 is
                  # mapped but not in service (as of Nov 2026).
                  "exclude": r"navi mumbai|line 11"},
    "bengaluru": {"label": "Bengaluru", "bbox": (12.75, 77.40, 13.20, 77.80),
                  "network": r"namma metro|bangalore metro|bengaluru metro|bmrcl"},
    "chennai":   {"label": "Chennai",   "bbox": (12.85, 80.05, 13.25, 80.35),
                  "network": r"chennai metro|cmrl",
                  # 2A is an inter-corridor service over Line 1/2 track.
                  "exclude": r"inter-corridor"},
    "hyderabad": {"label": "Hyderabad", "bbox": (17.25, 78.25, 17.60, 78.65),
                  "network": r"hyderabad metro|l&t metro"},
}

OVERPASS_URLS = [
    "https://overpass.kumi.systems/api/interpreter",  # more reliable in testing
    "https://overpass-api.de/api/interpreter",
]
ROUTE_TYPES = r"^(subway|light_rail|train)$"   # network regex keeps it to metro
NBINS = 36
H_GRID = math.log(4)
H_MAX = math.log(NBINS)
GEOD = Geod(ellps="WGS84")
FALLBACK_COLOURS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd",
                    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf"]


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def fetch_city(key, cfg, cache_dir, refresh=False):
    cache = cache_dir / f"{key}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text())
    s, w, n, e = cfg["bbox"]
    # Filter on the metro network server-side: otherwise long-distance train
    # routes crossing the bbox come back with country-length geometry.
    pat = cfg["network"].replace('"', '\\"')
    sel = f'relation["type"="route"]["route"~"{ROUTE_TYPES}"]'
    bbox = f"({s},{w},{n},{e})"
    q = f"""
    [out:json][timeout:180];
    (
      {sel}["network"~"{pat}",i]{bbox};
      {sel}["operator"~"{pat}",i]{bbox};
      {sel}["name"~"{pat}",i]{bbox};
    );
    out geom;
    """
    last_err = None
    for attempt in range(3):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": q}, timeout=240,
                                  headers={"User-Agent": "metro-orientation-script"})
                r.raise_for_status()
                data = r.json()
                cache.write_text(json.dumps(data))
                return data
            except Exception as err:  # try the next mirror, then back off
                last_err = err
                time.sleep(5)
        time.sleep(30 * (attempt + 1))  # Overpass rate limits (429) clear in ~a minute
    raise RuntimeError(f"Overpass failed for {key}: {last_err}")


def line_key(tags):
    """Collapse the two direction relations of a line into one key."""
    key = tags.get("ref") or re.split(r"[:(]|→|->|=>", tags.get("name", "unnamed"))[0]
    key = re.sub(r"\s+line\s*$", "", key.strip(), flags=re.I)  # "Blue Line" == "Blue"
    return key.strip()


def is_metro(tags, pattern, exclude=None):
    hay = " ".join(tags.get(k, "") for k in ("network", "operator", "name"))
    if exclude and re.search(exclude, hay, flags=re.I):
        return False
    if any(k in tags for k in ("proposed", "construction", "disused")) or \
            tags.get("state") in ("proposed", "construction", "disused"):
        return False
    return re.search(pattern, hay, flags=re.I) is not None


def extract_lines(data, pattern, all_tracks=False, exclude=None):
    """
    Returns {line: {"colour": str|None, "ways": {way_id: [(lon, lat), ...]}}}.
    Skips stop/platform members. In default mode keeps only the longest
    relation per line (one direction = roughly one centreline).
    """
    by_line = defaultdict(list)
    for el in data.get("elements", []):
        if el.get("type") != "relation":
            continue
        tags = el.get("tags", {})
        if not is_metro(tags, pattern, exclude):
            continue
        ways = {}
        for m in el.get("members", []):
            if m.get("type") != "way" or "geometry" not in m:
                continue
            role = m.get("role", "")
            if role and not role.startswith(("forward", "backward")):
                continue  # platforms, stops etc.
            coords = [(p["lon"], p["lat"]) for p in m["geometry"] if p]
            if len(coords) >= 2:
                ways[m["ref"]] = coords
        if ways:
            by_line[line_key(tags)].append(
                {"colour": tags.get("colour"), "ways": ways})

    lines = {}
    for key, rels in by_line.items():
        colour = next((r["colour"] for r in rels if r["colour"]), None)
        if all_tracks:
            merged = {}
            for r in rels:
                merged.update(r["ways"])
        else:
            merged = max(rels, key=lambda r: total_length(r["ways"]))["ways"]
        lines[key] = {"colour": colour, "ways": merged}
    return lines


# --------------------------------------------------------------------------
# Geometry and metrics
# --------------------------------------------------------------------------
def segments(coords):
    lon = np.array([c[0] for c in coords])
    lat = np.array([c[1] for c in coords])
    az, _, dist = GEOD.inv(lon[:-1], lat[:-1], lon[1:], lat[1:])
    keep = dist > 0
    return np.mod(az[keep], 360.0), dist[keep]


def total_length(ways):
    return sum(segments(c)[1].sum() for c in ways.values())


def histogram(bearings, weights):
    """Bidirectional, length-weighted, 36 bins centred on 0, 10, ... 350."""
    b = np.concatenate([bearings, np.mod(bearings + 180.0, 360.0)])
    w = np.concatenate([weights, weights])
    idx = np.floor(np.mod(b + 360.0 / NBINS / 2, 360.0) / (360.0 / NBINS)).astype(int)
    return np.bincount(idx, weights=w, minlength=NBINS)


def entropy(hist):
    p = hist / hist.sum()
    p = p[p > 0]
    return float(-(p * np.log(p)).sum())


def orientation_order(hist):
    h = entropy(hist)
    return 1.0 - ((h - H_GRID) / (H_MAX - H_GRID)) ** 2, h


AXIS_TOL = 15.0  # degrees either side of an axis counted as "on" it


def axial_distance(bearings, axis):
    """Smallest angle (0-90) between undirected bearings and an axis."""
    return np.abs(np.mod(bearings - axis + 90.0, 180.0) - 90.0)


def axis_stats(bearings, weights):
    """
    Rotation-aware companions to phi (which is rotation-invariant).

    R2 = |weighted mean of exp(2i*theta)|: 1 when all track lies on ONE axis,
         0 for a uniform spread AND for a perfect two-axis grid.
    R4 = |weighted mean of exp(4i*theta)|: 1 for one axis or a perfect
         right-angle grid, 0 for a uniform spread.
    Grid orientation comes from arg(m4)/4; the main axis is whichever of the
    two perpendicular arms carries more track. Shares are the fractions of
    track length within AXIS_TOL degrees of the main axis and its perpendicular.

    Reading them together:
      high R4, low R2, balanced shares  -> grid (Chicago-like)
      high R2, main share >> cross      -> one main direction (NYC-like)
      low R4                            -> no preferred directions (Paris-like)
    """
    th = np.radians(bearings)
    w = weights / weights.sum()
    m2 = np.sum(w * np.exp(2j * th))
    m4 = np.sum(w * np.exp(4j * th))
    grid_axis = np.mod(np.degrees(np.angle(m4)) / 4.0, 90.0)
    arms = [grid_axis, grid_axis + 90.0]
    shares = [float(w[axial_distance(bearings, a) <= AXIS_TOL].sum()) for a in arms]
    main = int(np.argmax(shares))
    main_axis = float(np.mod(arms[main], 180.0))
    if main_axis > 179.5:
        main_axis = 0.0
    return {"R2": float(abs(m2)), "R4": float(abs(m4)),
            "axis": float(main_axis),              # 0-180, compass bearing of main arm
            "tilt": float(min(grid_axis, 90.0 - grid_axis)),  # deviation from N-S/E-W
            "main_share": shares[main], "cross_share": shares[1 - main]}


# Label thresholds. These are judgement calls, set so that real (wiggly) track
# is classified sensibly; adjust them if you disagree with a label.
R4_AXES = 0.25      # R4 at or above this: track concentrates on perpendicular axes
R2_ONE_AXIS = 0.25  # R2 at or above this: track leans towards one axis
GRID_BALANCE = 0.5  # cross share >= this x main share: balanced grid
SKEW_BALANCE = 0.25 # cross share >= this x main share: lopsided grid


def shape_label(s):
    ratio = s["cross_share"] / s["main_share"] if s["main_share"] else 0.0
    if s["R4"] >= R4_AXES and ratio >= GRID_BALANCE:
        return "grid"
    if s["R4"] >= R4_AXES and ratio >= SKEW_BALANCE:
        return "skewed grid"
    if s["R2"] >= R2_ONE_AXIS:
        return "one main direction"
    if s["R4"] >= R4_AXES:
        return "one main direction"
    return "no dominant axis"


def null_phi(weights, reps=500, seed=0):
    """phi for the same segment lengths with uniformly random bearings."""
    rng = np.random.default_rng(seed)
    out = np.empty(reps)
    for i in range(reps):
        out[i] = orientation_order(histogram(rng.uniform(0, 360, len(weights)), weights))[0]
    return float(out.mean()), float(np.percentile(out, 95))


def analyse(lines):
    per_line_hist, all_b, all_w = {}, [], []
    for key, ln in lines.items():
        bs, ws = [], []
        for coords in ln["ways"].values():
            b, w = segments(coords)
            bs.append(b); ws.append(w)
        b, w = np.concatenate(bs), np.concatenate(ws)
        per_line_hist[key] = histogram(b, w)
        all_b.append(b); all_w.append(w)
    b, w = np.concatenate(all_b), np.concatenate(all_w)
    total = histogram(b, w)
    phi, h = orientation_order(total)
    null_mean, null_95 = null_phi(w)
    ax = axis_stats(b, w)
    return {"per_line": per_line_hist, "total": total, "phi": phi, "H": h,
            "n_segments": len(w), "km": w.sum() / 1000.0,
            "null_mean": null_mean, "null_95": null_95,
            **ax, "shape": shape_label(ax)}


# --------------------------------------------------------------------------
# Plotting
# --------------------------------------------------------------------------
def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def colour_for(lines):
    out, i = {}, 0
    for key in sorted(lines, key=natural_key):
        c = lines[key]["colour"]
        if not c or not re.match(r"^#?[0-9a-fA-F]{3,6}$|^[a-z]+$", c):
            c = FALLBACK_COLOURS[i % len(FALLBACK_COLOURS)]; i += 1
        elif re.match(r"^[0-9a-fA-F]{6}$", c):
            c = "#" + c
        out[key] = c
    return out


def draw_rose(ax, res, colours, rmax=None):
    """Stacked rose where bar AREA (not radius) is proportional to share."""
    total = res["total"].sum()
    width = 2 * np.pi / NBINS
    theta = np.arange(NBINS) * width
    cum = np.zeros(NBINS)
    for key in sorted(res["per_line"], key=natural_key):
        share = res["per_line"][key] / total
        r0, r1 = np.sqrt(cum), np.sqrt(cum + share)
        ax.bar(theta, r1 - r0, width=width * 0.95, bottom=r0,
               color=colours[key], edgecolor="white", linewidth=0.3)
        cum += share
    top = rmax or np.sqrt(cum.max()) * 1.05
    if res.get("shape", "no dominant axis") != "no dominant axis":  # mark the main axis (solid) and its perpendicular (dotted)
        for ang, style in ((res["axis"], "-"), (res["axis"] + 90.0, ":")):
            t = np.radians([ang, ang + 180.0])
            for tt in t:
                ax.plot([tt, tt], [0, top], ls=style, color="#333", lw=0.8, alpha=0.6)
    ax.set_theta_zero_location("N")
    ax.set_theta_direction(-1)
    ax.set_xticks([0, np.pi / 2, np.pi, 3 * np.pi / 2])
    ax.set_xticklabels(["N", "E", "S", "W"], fontsize=8, color="#666")
    ax.set_yticks([])
    ax.set_ylim(0, rmax or np.sqrt(cum.max()) * 1.05)
    ax.spines["polar"].set_color("#ccc")


def draw_map(ax, lines, colours):
    for key, ln in lines.items():
        for coords in ln["ways"].values():
            xs, ys = zip(*coords)
            ax.plot(xs, ys, color=colours[key], lw=1)
    lat0 = np.mean([c[1] for ln in lines.values() for w in ln["ways"].values() for c in w])
    ax.set_aspect(1 / math.cos(math.radians(lat0)))
    ax.axis("off")


def plot_city(label, lines, res, path):
    import matplotlib.pyplot as plt
    colours = colour_for(lines)
    fig = plt.figure(figsize=(7, 9), facecolor="#efe9df")
    fig.text(0.07, 0.95, label, fontsize=26, weight="bold")
    fig.text(0.07, 0.915, "Which way does the metro run?", fontsize=12)
    fig.text(0.07, 0.89,
             f"orientation order {res['phi']:.2f}  ·  random-bearing baseline "
             f"{res['null_mean']:.2f} (95th pct {res['null_95']:.2f})  ·  "
             f"{res['km']:.0f} km, {res['n_segments']} segments",
             fontsize=8, color="#555")
    fig.text(0.07, 0.872,
             f"{res['shape']}  ·  main axis {res['axis']:.0f}° "
             f"(tilt {res['tilt']:.0f}° off N–S/E–W)  ·  "
             f"{res['main_share']:.0%} of track on it, {res['cross_share']:.0%} across  ·  "
             f"R2 {res['R2']:.2f}, R4 {res['R4']:.2f}",
             fontsize=8, color="#555")
    ax = fig.add_axes([0.13, 0.31, 0.74, 0.51], projection="polar")
    ax.set_facecolor("white")
    draw_rose(ax, res, colours)
    draw_map(fig.add_axes([0.05, 0.05, 0.3, 0.25]), lines, colours)
    keys = sorted(lines, key=natural_key)
    lax = fig.add_axes([0.4, 0.05, 0.55, 0.25]); lax.axis("off")
    for i, k in enumerate(keys):
        col, row = divmod(i, 8)
        lax.add_patch(plt.Rectangle((col * 0.5, 1 - (row + 1) * 0.12), 0.05, 0.07,
                                    color=colours[k], transform=lax.transAxes))
        lax.text(col * 0.5 + 0.07, 1 - (row + 1) * 0.12 + 0.01, k[:28],
                 fontsize=7, transform=lax.transAxes)
    fig.text(0.07, 0.015, "Bar area = share of track length in that direction (bidirectional). "
             "Data © OpenStreetMap contributors. Method after Boeing (2019).",
             fontsize=6, color="#777")
    fig.savefig(path, dpi=200)
    plt.close(fig)


def plot_grid(results, path):
    import matplotlib.pyplot as plt
    items = sorted(results.items(), key=lambda kv: -kv[1][1]["phi"])
    ncol = 3
    nrow = math.ceil(len(items) / ncol)
    fig = plt.figure(figsize=(4 * ncol, 4.9 * nrow + 0.8), facecolor="#efe9df")
    fig.suptitle("Which way does the metro run? Indian cities", fontsize=16, weight="bold")
    for i, (label, (lines, res)) in enumerate(items):
        ax = fig.add_subplot(nrow, ncol, i + 1, projection="polar")
        ax.set_facecolor("white")
        draw_rose(ax, res, colour_for(lines))
        ax.set_title(f"{label}\norder {res['phi']:.2f}  (null {res['null_mean']:.2f})\n"
                     f"{res['shape']}, axis {res['axis']:.0f}°",
                     fontsize=10, pad=12)
    fig.subplots_adjust(left=0.05, right=0.95, bottom=0.03,
                        top=1 - 1.3 / (4.9 * nrow + 0.8), hspace=0.55, wspace=0.3)
    fig.savefig(path, dpi=200)
    plt.close(fig)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cities", nargs="*", default=list(CITIES))
    ap.add_argument("--all-tracks", action="store_true",
                    help="count every unique way, not one direction per line")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    out = Path(args.out); out.mkdir(exist_ok=True)
    cache = out / "cache"; cache.mkdir(exist_ok=True)
    results = {}
    for key in args.cities:
        cfg = CITIES[key]
        try:
            data = fetch_city(key, cfg, cache, args.refresh)
        except RuntimeError as err:
            print(f"{cfg['label']}: skipped ({err})")
            continue
        lines = extract_lines(data, cfg["network"], args.all_tracks, cfg.get("exclude"))
        if not lines:
            print(f"{cfg['label']}: no metro relations matched; check bbox/network regex")
            continue
        res = analyse(lines)
        results[cfg["label"]] = (lines, res)
        plot_city(cfg["label"], lines, res, out / f"{key}.png")
        print(f"{cfg['label']:<10} phi={res['phi']:.3f}  H={res['H']:.3f}  "
              f"null={res['null_mean']:.3f} (95th {res['null_95']:.3f})  "
              f"{res['km']:.0f} km  lines={len(lines)}  |  {res['shape']}, "
              f"axis {res['axis']:.0f}°, main {res['main_share']:.0%} / "
              f"cross {res['cross_share']:.0%}, R2={res['R2']:.2f} R4={res['R4']:.2f}")

    if results:
        plot_grid(results, out / "grid.png")
        with open(out / "summary.csv", "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["city", "phi", "entropy", "null_phi_mean", "null_phi_95",
                         "km", "segments", "lines", "shape", "main_axis_deg",
                         "tilt_deg", "main_share", "cross_share", "R2", "R4"])
            for label, (lines, r) in results.items():
                wr.writerow([label, f"{r['phi']:.4f}", f"{r['H']:.4f}",
                             f"{r['null_mean']:.4f}", f"{r['null_95']:.4f}",
                             f"{r['km']:.1f}", r["n_segments"], len(lines),
                             r["shape"], f"{r['axis']:.1f}", f"{r['tilt']:.1f}",
                             f"{r['main_share']:.4f}", f"{r['cross_share']:.4f}",
                             f"{r['R2']:.4f}", f"{r['R4']:.4f}"])


if __name__ == "__main__":
    main()
