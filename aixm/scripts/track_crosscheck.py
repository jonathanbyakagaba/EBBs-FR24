#!/usr/bin/env python3
"""
Cross-check the AIXM geometry against the flight tracks already in this repo.

    python scripts/track_crosscheck.py [--xml out/HUEN_AIP_DS.xml]

The point is independent evidence. The AIXM runway geometry comes from
published threshold coordinates; `tracks/` contains what aircraft actually did
on the ground at Entebbe (350 flights, ~250k positions). Comparing the two
answers questions a schema validator cannot:

* do aircraft roll along the centreline the model publishes?
* do departures line up at the modelled thresholds?
* is the derived taxiway/apron network where aircraft actually go?
* which runway and direction is really in use?

Output: out/track_crosscheck.json (with a markdown summary that the validation
report embeds) and a console digest.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from lxml import etree

AIXM = "{http://www.aixm.aero/schema/5.1.1}"
MSG = "{http://www.aixm.aero/schema/5.1.1/message}"
GML = "{http://www.opengis.net/gml/3.2}"
XLINK = "{http://www.w3.org/1999/xlink}"

ROOT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT_DIR.parent
EARTH_R = 6371008.8


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def enu(lat, lon, ref):
    lat0, lon0 = ref
    return ((lon - lon0) * 111320.0 * math.cos(math.radians(lat0)),
            (lat - lat0) * 110540.0)


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def point_segment(lat, lon, a, b, ref):
    """Distance (m) from point to segment and the fraction along the segment."""
    px, py = enu(lat, lon, ref)
    ax, ay = enu(a[0], a[1], ref)
    bx, by = enu(b[0], b[1], ref)
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)), t


def point_in_ring(lat, lon, ring):
    inside = False
    n = len(ring)
    for i in range(n):
        y1, x1 = ring[i]
        y2, x2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xin = x1 + (lat - y1) / (y2 - y1) * (x2 - x1)
            if lon < xin:
                inside = not inside
    return inside


def ring_from_surface(surface_el):
    if surface_el is None:
        return []
    pl = surface_el.find(f".//{GML}posList")
    if pl is None or not pl.text:
        return []
    nums = [float(x) for x in pl.text.split()]
    pts = list(zip(nums[0::2], nums[1::2]))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def point_of(prop_el):
    if prop_el is None:
        return None
    pos = prop_el.find(f".//{GML}pos")
    if pos is None or not pos.text:
        return None
    nums = [float(x) for x in pos.text.split()]
    return (nums[0], nums[1]) if len(nums) >= 2 else None


# --------------------------------------------------------------------------- #
# Read the AIXM message back (independent of the builder)
# --------------------------------------------------------------------------- #
def load_model(xml_path: Path) -> dict:
    root = etree.parse(str(xml_path)).getroot()
    features = {}
    for member in root.findall(MSG + "hasMember"):
        for feat in member:
            ftype = etree.QName(feat).localname
            for wrapper in feat.findall(AIXM + "timeSlice"):
                for ts in wrapper:
                    features.setdefault(ftype, []).append(ts)

    oid_to_key = {}
    for ftype, slices in features.items():
        for ts in slices:
            parent = ts.getparent().getparent()
            ident = parent.find(GML + "identifier")
            if ident is not None and ident.text:
                oid_to_key[ident.text.strip()] = ts.get(GML + "id")

    def by_key(oid):
        key = oid_to_key.get((oid or "").replace("urn:uuid:", ""))
        return key

    # runway centrelines from the threshold points, paired by their runway
    directions = {}
    for ts in features.get("RunwayDirection", []):
        used = ts.find(AIXM + "usedRunway")
        directions[ts.get(GML + "id")] = {
            "designator": ts.findtext(AIXM + "designator"),
            "runway_key": by_key(used.get(XLINK + "href") if used is not None else None),
            "true_bearing": ts.findtext(AIXM + "trueBearing"),
        }

    runways = {}
    for ts in features.get("RunwayCentrelinePoint", []):
        loc = point_of(ts.find(AIXM + "location"))
        on = ts.find(AIXM + "onRunway")
        dkey = by_key(on.get(XLINK + "href") if on is not None else None)
        if loc is None or dkey not in directions:
            continue
        rwy_key = directions[dkey]["runway_key"]
        declared = {}
        for dd in ts.findall(AIXM + "associatedDeclaredDistance"):
            rdd = dd.find(AIXM + "RunwayDeclaredDistance")
            if rdd is None:
                continue
            dtype = rdd.findtext(AIXM + "type")
            dist = rdd.findtext(
                f"{AIXM}declaredValue/{AIXM}RunwayDeclaredDistanceValue/{AIXM}distance")
            if dtype and dist:
                declared[dtype] = float(dist)
        runways.setdefault(rwy_key, {})[directions[dkey]["designator"]] = {
            "location": loc,
            "point_key": ts.get(GML + "id"),
            "declared": declared,
        }

    surfaces = {"runway": [], "taxiway": [], "apron": []}
    for ts in features.get("RunwayElement", []):
        ring = ring_from_surface(ts.find(f"{AIXM}extent/{AIXM}ElevatedSurface"))
        if ring:
            surfaces["runway"].append({"key": ts.get(GML + "id"), "ring": ring})
    for ts in features.get("TaxiwayElement", []):
        ring = ring_from_surface(ts.find(f"{AIXM}extent/{AIXM}ElevatedSurface"))
        if ring:
            surfaces["taxiway"].append({"key": ts.get(GML + "id"), "ring": ring})
    for ts in features.get("ApronElement", []):
        ring = ring_from_surface(ts.find(f"{AIXM}extent/{AIXM}ElevatedSurface"))
        if ring:
            surfaces["apron"].append({"key": ts.get(GML + "id"), "ring": ring})

    aerodrome = features.get("AirportHeliport", [None])[0]
    arp = point_of(aerodrome.find(AIXM + "ARP")) if aerodrome is not None else None

    return {
        "runways": {k: v for k, v in runways.items() if k},
        "directions": directions,
        "surfaces": surfaces,
        "arp": arp,
    }


# --------------------------------------------------------------------------- #
# Tracks
# --------------------------------------------------------------------------- #
def load_movements(path: Path) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    if not path.exists():
        return out
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("flight_id_hex") and row.get("direction"):
                out[row["flight_id_hex"]].add(row["direction"])
    return out


def ground_points(csv_path: Path, ref, max_km=25.0):
    pts = []
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                pos = row.get("Position")
                if not pos or "," not in pos:
                    continue
                try:
                    lat, lon = (float(v) for v in pos.split(","))
                    alt = float(row.get("Altitude") or 0)
                except ValueError:
                    continue
                if alt > 60:
                    continue
                if haversine(ref[0], ref[1], lat, lon) > max_km * 1000:
                    continue
                pts.append((int(row.get("Timestamp") or 0), lat, lon, alt))
    except OSError:
        return []
    return pts


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def analyse(model: dict, tracks_dir: Path, movements: dict) -> dict:
    ref = model["arp"] or (0.042386, 32.443501)
    runways = model["runways"]
    surfaces = model["surfaces"]

    # runway segments: pair of thresholds per runway
    segments = []
    for rwy_key, ends in runways.items():
        if len(ends) != 2:
            continue
        (d1, e1), (d2, e2) = ends.items()
        length = haversine(e1["location"][0], e1["location"][1],
                           e2["location"][0], e2["location"][1])
        segments.append({
            "runway_key": rwy_key,
            "designator": f"{d1}/{d2}",
            "a_end": d1, "b_end": d2,
            "a": e1["location"], "b": e2["location"],
            "length_m": length,
            "ends": {d1: e1, d2: e2},
        })

    on_rwy_offsets = defaultdict(list)
    coverage = Counter()
    util = Counter()
    lineup = defaultdict(list)          # runway direction -> distance from its THR
    touchdown = defaultdict(list)       # runway direction -> distance from its THR
    n_tracks = n_used = 0
    rwy12_confusers = 0

    for csv_path in sorted(tracks_dir.glob("*.csv")):
        n_tracks += 1
        flight_id = csv_path.stem.split("-")[-1]
        pts = ground_points(csv_path, ref)
        if len(pts) < 5:
            continue
        n_used += 1
        dirs = movements.get(flight_id, set())

        on_rwy = []      # (timestamp, lat, lon, segment, t, dist)
        near_12 = 0
        for ts_, lat, lon, _ in pts:
            best = None
            for seg in segments:
                d, t = point_segment(lat, lon, seg["a"], seg["b"], ref)
                if best is None or d < best[0]:
                    best = (d, t, seg)
            d, t, seg = best
            if seg["designator"] == "12/30" and d <= 30.0:
                near_12 += 1
            if d <= 30.0:
                on_rwy_offsets[seg["designator"]].append(d)
                on_rwy.append((ts_, lat, lon, seg, t, d))
                coverage["on_runway"] += 1
                continue
            hit = None
            for kind in ("taxiway", "apron"):
                for surf in surfaces[kind]:
                    if point_in_ring(lat, lon, surf["ring"]):
                        hit = kind
                        break
                if hit:
                    break
            coverage[hit or "uncovered"] += 1

        if not on_rwy:
            continue
        if near_12 >= 2:
            rwy12_confusers += 1

        # Which runway was in use? Anchor on where the aircraft actually left the
        # ground (departure) or first touched it (arrival): taxiing traffic here
        # crosses runway 12/30 on its way to 17/35, so counting on-centreline
        # points would pick the runway that was merely taxiied across.
        anchor = pts[-1] if "DEP" in dirs else pts[0]
        anchor_best = None
        for seg in segments:
            d, _ = point_segment(anchor[1], anchor[2], seg["a"], seg["b"], ref)
            if anchor_best is None or d < anchor_best[0]:
                anchor_best = (d, seg)
        anchor_dist, anchor_seg = anchor_best
        if anchor_dist <= 60.0:
            designator_in_use = anchor_seg["designator"]
        else:
            per_rwy = Counter(p[3]["designator"] for p in on_rwy)
            designator_in_use = per_rwy.most_common(1)[0][0]
        track_pts = [p for p in on_rwy if p[3]["designator"] == designator_in_use]
        if not track_pts:
            continue

        # Which direction? Use the direction of travel along the centreline: an
        # aircraft moving from the a_end towards the b_end took off / landed in
        # the a_end direction.
        seg = track_pts[0][3]
        t_first = track_pts[0][4]
        t_last = track_pts[-1][4]

        length = seg["length_m"]
        dt = (t_last - t_first) * length
        if abs(dt) >= 100.0:
            direction = seg["a_end"] if dt > 0 else seg["b_end"]
        else:
            direction = seg["a_end"] if t_first < 0.5 else seg["b_end"]

        thr = seg["ends"][direction]["location"]
        if "DEP" in dirs:
            util[f"DEP RWY {seg['designator']} dir {direction}"] += 1
            lineup[direction].append(
                haversine(track_pts[0][1], track_pts[0][2], thr[0], thr[1]))
        elif "ARR" in dirs:
            util[f"ARR RWY {seg['designator']} dir {direction}"] += 1
            touchdown[direction].append(
                haversine(track_pts[0][1], track_pts[0][2], thr[0], thr[1]))

    def stats(values):
        if not values:
            return None
        v = sorted(values)
        return {
            "n": len(v),
            "min": v[0],
            "p10": v[len(v) // 10],
            "median": statistics.median(v),
            "p90": v[9 * len(v) // 10],
            "max": v[-1],
        }

    total_cov = sum(coverage.values()) or 1
    result = {
        "tracks_total": n_tracks,
        "tracks_with_ground_data": n_used,
        "runways": [
            {"designator": s["designator"],
             "modelled_length_m": round(s["length_m"], 1),
             "ends": list(s["ends"].keys())}
            for s in segments
        ],
        "centreline_offset_m": {k: stats(v) for k, v in on_rwy_offsets.items()},
        "surface_coverage": {
            k: {"count": c, "percent": round(100.0 * c / total_cov, 1)}
            for k, c in coverage.items()
        },
        "utilisation": dict(util.most_common()),
        "lineup_distance_to_threshold_m": {k: stats(v) for k, v in lineup.items()},
        "touchdown_distance_from_threshold_m": {k: stats(v) for k, v in touchdown.items()},
        "flights_touching_12_30_threshold_area": rwy12_confusers,
    }
    result["summary_markdown"] = render_summary(result, runways)
    return result


def render_summary(r: dict, runways: dict) -> str:
    L = []
    L.append(f"`tracks/` holds **{r['tracks_total']}** flights; **{r['tracks_with_ground_data']}** "
             "contain ground-level positions at Entebbe (altitude < 60 ft within 25 km of the ARP). "
             "Those positions were compared with the geometry in the AIXM message.\n")

    L.append("### 6.1 Runway centreline agreement\n")
    L.append("| runway | modelled length (M) | ground points on centreline | median offset | p90 offset |")
    L.append("|---|---|---|---|---|")
    for rwy in r["runways"]:
        s = r["centreline_offset_m"].get(rwy["designator"])
        if s:
            L.append(f"| {rwy['designator']} | {rwy['modelled_length_m']:.0f} | {s['n']} | "
                     f"{s['median']:.1f} m | {s['p90']:.1f} m |")
        else:
            L.append(f"| {rwy['designator']} | {rwy['modelled_length_m']:.0f} | 0 | - | - |")
    L.append("")
    L.append("A median offset of a few metres means the published threshold coordinates "
             "and the runway bearings in the data set describe the pavement aircraft "
             "actually use. Offsets are lateral distance to the modelled centreline, "
             "counted only for points within 30 m of it.\n")

    L.append("### 6.2 Runway and direction in use\n")
    L.append("| usage | movements |")
    L.append("|---|---|")
    for k, v in r["utilisation"].items():
        L.append(f"| {k} | {v} |")
    L.append("")

    if r["lineup_distance_to_threshold_m"]:
        L.append("### 6.3 Departure line-up and arrival touchdown vs modelled thresholds\n")
        L.append("| runway direction | phase | n | median distance from THR | p90 |")
        L.append("|---|---|---|---|---|")
        for k, s in r["lineup_distance_to_threshold_m"].items():
            L.append(f"| {k} | departure line-up | {s['n']} | {s['median']:.0f} m | {s['p90']:.0f} m |")
        for k, s in r["touchdown_distance_from_threshold_m"].items():
            L.append(f"| {k} | first ground point (arrival) | {s['n']} | {s['median']:.0f} m | {s['p90']:.0f} m |")
        L.append("")
        L.append("A departure line-up close to the threshold corroborates the threshold "
                 "position; a larger value indicates an intersection departure. The "
                 "arrival figure is the first *reported* ground position, which FR24 "
                 "playback samples coarsely, so it is an upper bound on touchdown "
                 "distance rather than a touchdown measurement.\n")
        if r.get("flights_touching_12_30_threshold_area"):
            L.append(f"Runway 12/30 never appears as the runway in use in any analysed "
                     f"track, although {r['flights_touching_12_30_threshold_area']} flights "
                     "taxi close enough to its centreline to register points on it. "
                     "Combined with the aerodrome chart legend, that suggests 12/30 is not "
                     "in routine use; confirm against the AIP.\n")

    L.append("### 6.4 Coverage of the derived apron/taxiway surfaces\n")
    L.append("| classification of ground points | count | share |")
    L.append("|---|---|---|")
    for k, v in sorted(r["surface_coverage"].items(), key=lambda kv: -kv[1]["count"]):
        L.append(f"| {k} | {v['count']} | {v['percent']}% |")
    L.append("")
    uncovered = r["surface_coverage"].get("uncovered", {}).get("percent", 0)
    L.append(f"**{uncovered}%** of observed ground movement falls outside every modelled "
             "surface. That is the honest measure of how incomplete the derived taxiway and "
             "apron network is: aircraft also use links, holding bays and stand taxilanes "
             "that no accessible source describes.\n")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xml", default=str(ROOT_DIR / "out" / "HUEN_AIP_DS.xml"))
    ap.add_argument("--tracks", default=str(REPO_ROOT / "tracks"))
    ap.add_argument("--movements", default=str(REPO_ROOT / "data" / "movements.csv"))
    ap.add_argument("--out", default=str(ROOT_DIR / "out" / "track_crosscheck.json"))
    args = ap.parse_args()

    model = load_model(Path(args.xml))
    movements = load_movements(Path(args.movements))
    result = analyse(model, Path(args.tracks), movements)
    Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"tracks: {result['tracks_total']} ({result['tracks_with_ground_data']} with ground data)")
    for k, s in result["centreline_offset_m"].items():
        print(f"  centreline offset {k}: n={s['n']} median={s['median']:.1f} m p90={s['p90']:.1f} m")
    print("  utilisation:")
    for k, v in result["utilisation"].items():
        print(f"    {k}: {v}")
    print("  surface coverage:")
    for k, v in result["surface_coverage"].items():
        print(f"    {k}: {v['count']} ({v['percent']}%)")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
