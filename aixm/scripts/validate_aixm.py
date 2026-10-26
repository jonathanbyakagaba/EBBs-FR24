#!/usr/bin/env python3
"""
Validate the generated AIXM 5.1.1 aerodrome data set and write a report.

    python scripts/validate_aixm.py [--xml out/HUEN_AIP_DS.xml]

Two layers:

1. **Schema** - the message is validated against the official AIXM 5.1.1
   schemas vendored in schema/ (AIXM_Features, AIXM_DataTypes, GML 3.2.1,
   ISO 19139, xlink). A file that passes is a structurally legal AIXM 5.1.1
   BasicMessage.

2. **Domain** - the schema cannot know that a TORA must not exceed the
   runway, that two opposite runway directions must differ by 180 degrees, or
   that an LDA shorter than the TORA implies a displaced threshold. Those
   checks live here, and they are the ones that catch real coding errors.

Findings are graded ERROR (dataset is wrong), WARN (data conflict or missing
evidence - needs the AIP), INFO (worth knowing).

Exit status is non-zero if the schema fails or any ERROR is raised, so this can
be wired into CI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

AIXM = "{http://www.aixm.aero/schema/5.1.1}"
MSG = "{http://www.aixm.aero/schema/5.1.1/message}"
GML = "{http://www.opengis.net/gml/3.2}"
XLINK = "{http://www.w3.org/1999/xlink}"
GMD = "{http://www.isotc211.org/2005/gmd}"

ROOT_DIR = Path(__file__).resolve().parents[1]
SCHEMA = ROOT_DIR / "schema" / "aixm-5.1.1" / "message" / "AIXM_BasicMessage.xsd"
EARTH_R = 6371008.8
LEN_TOL_M = 30.0      # tolerance when comparing modelled vs published lengths
LEN_TOL_REL = 0.01    # ... or 1 %, whichever is larger


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    severity: str          # ERROR | WARN | INFO
    check: str
    object: str
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    structural: list[tuple[str, str, int, int]] = field(default_factory=list)
    # (check name, status PASS/FAIL, checked count, failed count)

    def add(self, severity: str, check: str, obj: str, message: str) -> None:
        self.findings.append(Finding(severity, check, obj, message))

    def error(self, check, obj, msg):
        self.add("ERROR", check, obj, msg)

    def warn(self, check, obj, msg):
        self.add("WARN", check, obj, msg)

    def info(self, check, obj, msg):
        self.add("INFO", check, obj, msg)

    def structural_check(self, name: str, failures: list[str], checked: int) -> None:
        status = "PASS" if not failures else "FAIL"
        self.structural.append((name, status, checked, len(failures)))
        for f in failures:
            self.add("ERROR", name, f, "structural check failed")

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def haversine(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff(a: float, b: float) -> float:
    return ((a - b + 180) % 360) - 180


def point_in_ring(lat: float, lon: float, ring: list[tuple[float, float]]) -> bool:
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


def dist_to_ring(lat: float, lon: float, ring: list[tuple[float, float]]) -> float:
    """Shortest distance in metres from a point to a ring's edges."""
    lat0 = lat
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    px, py = lon * kx, lat * ky
    best = float("inf")
    n = len(ring)
    for i in range(n):
        y1, x1 = ring[i]
        y2, x2 = ring[(i + 1) % n]
        ax, ay, bx, by = x1 * kx, y1 * ky, x2 * kx, y2 * ky
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        tt = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
        best = min(best, math.hypot(px - (ax + tt * dx), py - (ay + tt * dy)))
    return best


def on_or_in_ring(lat: float, lon: float, ring: list[tuple[float, float]],
                  tol_m: float = 1.0) -> bool:
    """Inside the ring, or within `tol_m` of its boundary.

    Thresholds sit exactly on the pavement edge, so a strict point-in-polygon
    test is the wrong predicate for "is this threshold on the runway".
    """
    return point_in_ring(lat, lon, ring) or dist_to_ring(lat, lon, ring) <= tol_m


def ring_area_m2(ring: list[tuple[float, float]]) -> float:
    """Shoelace area in m^2 using a local equirectangular projection."""
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    kx = 111320.0 * math.cos(math.radians(lat0))
    ky = 110540.0
    pts = [((lon) * kx, (lat) * ky) for lat, lon in ring]
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


# --------------------------------------------------------------------------- #
# Parse the message into something checkable
# --------------------------------------------------------------------------- #
@dataclass
class Feature:
    key: str                 # gml:id of the timeslice (human-readable)
    type: str                # AIXM feature type name
    element: etree._Element  # the feature element
    timeslice: etree._Element
    oid: str                 # uuid string

    def text(self, prop: str) -> str | None:
        node = self.timeslice.find(AIXM + prop)
        return node.text.strip() if node is not None and node.text else None

    def node(self, prop: str):
        return self.timeslice.find(AIXM + prop)

    def nodes(self, prop: str):
        return self.timeslice.findall(AIXM + prop)

    def ref(self, prop: str) -> str | None:
        node = self.timeslice.find(AIXM + prop)
        if node is None:
            return None
        return node.get(XLINK + "href")


def parse_features(root) -> list[Feature]:
    out = []
    for member in root.findall(MSG + "hasMember"):
        for feat in member:
            tagname = etree.QName(feat).localname if isinstance(feat.tag, str) else "?"
            ident = feat.find(GML + "identifier")
            oid = ident.text.strip() if ident is not None and ident.text else ""
            # <aixm:timeSlice> is a wrapper; the concrete *TimeSlice element with
            # the gml:id and the feature properties is its only child.
            found = False
            for wrapper in feat.findall(AIXM + "timeSlice"):
                for ts in wrapper:
                    found = True
                    out.append(Feature(ts.get(GML + "id") or "?", tagname, feat, ts, oid))
            if not found:
                out.append(Feature("?", tagname, feat, feat, oid))
    return out


def urn_to_oid(href: str | None) -> str | None:
    if not href:
        return None
    m = re.match(r"urn:uuid:(.+)$", href.strip())
    return m.group(1) if m else None


def poslist_ring(surface_el) -> list[tuple[float, float]]:
    pl = surface_el.find(f".//{GML}posList")
    if pl is None or not pl.text:
        return []
    nums = [float(x) for x in pl.text.split()]
    pts = list(zip(nums[0::2], nums[1::2]))
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def point_of(prop_el) -> tuple[float, float] | None:
    if prop_el is None:
        return None
    pos = prop_el.find(f".//{GML}pos")
    if pos is None or not pos.text:
        return None
    nums = [float(x) for x in pos.text.split()]
    if len(nums) < 2:
        return None
    return (nums[0], nums[1])


def surface_of(ts, prop: str):
    holder = ts.find(AIXM + prop)
    if holder is None:
        return None
    return holder.find(AIXM + "ElevatedSurface")


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def check_schema(xml_path: Path, report: Report) -> bool:
    try:
        schema = etree.XMLSchema(etree.parse(str(SCHEMA)))
    except etree.XMLSchemaParseError as exc:      # pragma: no cover
        report.error("schema-compile", str(SCHEMA.name), str(exc)[:400])
        return False
    doc = etree.parse(str(xml_path))
    ok = schema.validate(doc)
    for err in schema.error_log:
        report.error("xsd", f"line {err.line}", err.message[:400])
    return ok


def check_structure(features: list[Feature], root, report: Report) -> None:
    # unique gml:ids across the whole document
    all_ids = [el.get(GML + "id") for el in root.iter() if el.get(GML + "id")]
    dupes = {i for i in all_ids if all_ids.count(i) > 1}
    report.structural_check("unique gml:id values", sorted(dupes), len(all_ids))
    for d in sorted(dupes):
        report.error("unique-id", d, "gml:id is used more than once")

    # xlink resolution
    oids = {f.oid for f in features if f.oid}
    dangling = []
    for el in root.iter():
        href = el.get(XLINK + "href")
        if not href:
            continue
        target = urn_to_oid(href)
        if target and target not in oids:
            dangling.append(f"{etree.QName(el).localname} -> {href}")
    report.structural_check("xlink references resolve", dangling, len(oids))
    for d in dangling[:20]:
        report.error("dangling-xlink", d, "reference target is not present in the message")

    # BASELINE conventions
    bad = []
    for f in features:
        if f.text("interpretation") != "BASELINE":
            bad.append(f"{f.type} {f.key}: interpretation={f.text('interpretation')}")
        if f.timeslice.find(GML + "validTime") is None:
            bad.append(f"{f.type} {f.key}: no gml:validTime")
        if f.text("sequenceNumber") is None:
            bad.append(f"{f.type} {f.key}: no sequenceNumber")
    report.structural_check("BASELINE time slices", bad, len(features))
    for b in bad:
        report.error("baseline", b, "BASELINE data set conventions not met")

    # one time slice per feature object
    multi = []
    for member in root.findall(MSG + "hasMember"):
        for feat in member:
            n = len(feat.findall(AIXM + "timeSlice"))
            if n != 1:
                multi.append(f"{etree.QName(feat).localname}: {n} time slices")
    report.structural_check("single time slice per feature", multi, len(features))
    for m_ in multi:
        report.warn("temporality", m_, "more than one time slice: only BASELINE is modelled")


def check_runways(features: list[Feature], report: Report) -> None:
    runways = {f.key: f for f in features if f.type == "Runway"}
    directions = [f for f in features if f.type == "RunwayDirection"]
    elements = {f.key: f for f in features if f.type == "RunwayElement"}
    thrs = [f for f in features if f.type == "RunwayCentrelinePoint"]

    # map runway key -> {designator: {lat, lon, bearing, declared}}
    by_runway: dict[str, dict] = {}
    for d in directions:
        rwy_href = d.ref("usedRunway")
        rwy_oid = urn_to_oid(rwy_href)
        rwy_key = next((k for k, f in runways.items() if f.oid == rwy_oid), None)
        if rwy_key is None:
            report.error("runway-ref", d.key, "usedRunway does not resolve to a Runway")
            continue
        designator = d.text("designator") or "?"
        by_runway.setdefault(rwy_key, {})[designator] = {
            "feature": d,
            "true_bearing": float(d.text("trueBearing") or "nan"),
            "magnetic_bearing": (float(d.text("magneticBearing"))
                                 if d.text("magneticBearing") else None),
        }

    for rwy_key, rwy in runways.items():
        designator = rwy.text("designator") or "?"
        nominal = float(rwy.text("nominalLength") or "nan")

        # thresholds on this runway
        thr = {}
        for t in thrs:
            on = urn_to_oid(t.ref("onRunway"))
            direction = next((d for d in directions if d.oid == on), None)
            if direction is None:
                continue
            if urn_to_oid(direction.ref("usedRunway")) != rwy.oid:
                continue
            loc = point_of(t.node("location"))
            if loc is None:
                continue
            declared = {}
            for dd in t.nodes("associatedDeclaredDistance"):
                rdd = dd.find(AIXM + "RunwayDeclaredDistance")
                if rdd is None:
                    continue
                dtype = rdd.findtext(AIXM + "type")
                dist = rdd.findtext(f"{AIXM}declaredValue/{AIXM}RunwayDeclaredDistanceValue/{AIXM}distance")
                if dtype and dist:
                    declared[dtype] = float(dist)
            thr[direction.text("designator")] = {
                "lat": loc[0], "lon": loc[1], "declared": declared,
                "role": t.text("role"), "key": t.key,
                "direction": direction,
            }
            if t.text("role") != "THR":
                report.warn("centreline-role", t.key,
                            f"role is {t.text('role')} where THR was expected at the "
                            "approach end of this direction")

        if len(thr) != 2:
            report.error("runway-ends", rwy_key,
                         f"{designator}: expected 2 threshold points, found {len(thr)}")
            continue

        ends = list(thr.items())
        (d1, e1), (d2, e2) = ends

        # 1. published length vs geodesic distance between threshold coordinates
        geo_len = haversine(e1["lat"], e1["lon"], e2["lat"], e2["lon"])
        tol = max(LEN_TOL_M, nominal * LEN_TOL_REL)
        if abs(geo_len - nominal) > tol:
            report.warn("runway-length", rwy_key,
                        f"{designator}: geodesic distance between threshold coordinates is "
                        f"{geo_len:.1f} M but nominalLength is {nominal:.0f} M "
                        f"(difference {geo_len - nominal:+.1f} M > {tol:.0f} M tolerance). "
                        "Published end coordinates are rounded to ~11 m in the source.")
        else:
            report.info("runway-length", rwy_key,
                        f"{designator}: geodesic {geo_len:.1f} M vs nominal {nominal:.0f} M "
                        f"({geo_len - nominal:+.1f} M) - within tolerance.")

        # 2. direction bearings
        for desig, end in ends:
            pub = end["direction"].text("trueBearing")
            if pub is None:
                report.warn("bearing", end["key"], f"{desig}: no trueBearing")
                continue
            other = e2 if desig == d1 else e1
            geo = bearing(end["lat"], end["lon"], other["lat"], other["lon"])
            delta = angle_diff(float(pub), geo)
            if abs(delta) > 1.0:
                report.warn("bearing", end["key"],
                            f"RWY {desig}: published true bearing {pub} DEG differs from the "
                            f"geodesic bearing between threshold coordinates ({geo:.2f} DEG) "
                            f"by {delta:+.2f} DEG.")
            else:
                report.info("bearing", end["key"],
                            f"RWY {desig}: published {pub} DEG vs geodesic {geo:.2f} DEG "
                            f"({delta:+.2f} DEG).")

        # 3. reciprocal directions
        b1 = float(ends[0][1]["direction"].text("trueBearing") or "nan")
        b2 = float(ends[1][1]["direction"].text("trueBearing") or "nan")
        rec = angle_diff(b1, (b2 + 180.0) % 360.0)
        if abs(rec) > 0.5:
            report.error("reciprocal", rwy_key,
                         f"{designator}: runway directions {d1}={b1} and {d2}={b2} are not "
                         f"reciprocal (off by {rec:+.2f} DEG).")
        else:
            report.info("reciprocal", rwy_key,
                        f"{designator}: reciprocal bearings consistent ({rec:+.2f} DEG).")

        # 4. declared distances
        for desig, end in ends:
            dec = end["declared"]
            missing = {"TORA", "TODA", "ASDA", "LDA"} - set(dec)
            if missing:
                report.warn("declared-distance", end["key"],
                            f"RWY {desig}: missing declared distance(s) {sorted(missing)}")
            tora = dec.get("TORA")
            if tora is not None and tora > nominal + tol:
                report.error("declared-distance", end["key"],
                             f"RWY {desig}: TORA {tora:.0f} M exceeds nominal runway length "
                             f"{nominal:.0f} M.")
            if tora is not None:
                for dt in ("TODA", "ASDA"):
                    if dec.get(dt) is not None and dec[dt] < tora:
                        report.error("declared-distance", end["key"],
                                     f"RWY {desig}: {dt} {dec[dt]:.0f} M is shorter than "
                                     f"TORA {tora:.0f} M without a stated stopway/clearway.")
            lda = dec.get("LDA")
            if tora is not None and lda is not None and lda < tora - 1.0:
                report.warn("declared-distance", end["key"],
                            f"RWY {desig}: LDA {lda:.0f} M is {tora - lda:.0f} M shorter than "
                            f"TORA {tora:.0f} M, but no displaced threshold (DISTHR) is encoded. "
                            "Either the threshold is displaced or the published LDA needs "
                            "confirming against AIP AD 2.13.")
            if lda is not None and lda > nominal + tol:
                report.error("declared-distance", end["key"],
                             f"RWY {desig}: LDA {lda:.0f} M exceeds runway length {nominal:.0f} M.")

        # 5. thresholds inside the pavement outline
        el = elements.get(rwy_key + "_ELEMENT")
        if el is not None:
            surf = surface_of(el.timeslice, "extent")
            ring = poslist_ring(surf) if surf is not None else []
            if not ring:
                report.error("pavement", el.key, "RunwayElement has no extent geometry")
            else:
                for desig, end in ends:
                    if not on_or_in_ring(end["lat"], end["lon"], ring):
                        report.error("pavement", end["key"],
                                     f"RWY {desig} threshold lies outside the runway "
                                     "pavement outline")
                report.info("pavement", el.key,
                            f"runway pavement area {ring_area_m2(ring):,.0f} m2, "
                            f"{len(ring)} vertices")

        # 6. precision approach consistency
        for desig, end in ends:
            pa = end["direction"].text("precisionApproachGuidance")
            amt = end["direction"].text("approachMarkingType")
            if pa and pa.startswith("ILS") and amt != "PRECISION":
                report.warn("approach", end["key"],
                            f"RWY {desig}: precisionApproachGuidance={pa} but "
                            f"approachMarkingType={amt}.")


def check_aerodrome(features: list[Feature], report: Report) -> None:
    ahp = next((f for f in features if f.type == "AirportHeliport"), None)
    if ahp is None:
        report.error("aerodrome", "-", "no AirportHeliport in the message")
        return
    elev = ahp.text("fieldElevation")
    arp = point_of(ahp.node("ARP"))
    if arp is None:
        report.error("aerodrome", ahp.key, "no ARP geometry")
    if elev is None:
        report.warn("aerodrome", ahp.key, "no field elevation")
    if ahp.text("controlType") == "JOINT":
        report.info("aerodrome", ahp.key, "controlType JOINT (civil/military joint use).")

    # runway end elevations vs aerodrome elevation
    aerodrome_elev = float(elev) if elev else None
    for f in features:
        if f.type != "RunwayCentrelinePoint":
            continue
        loc = point_of(f.node("location"))
        if loc is None:
            continue
        e = f.timeslice.findtext(f"{AIXM}location/{AIXM}ElevatedPoint/{AIXM}elevation")
        if e is None or aerodrome_elev is None:
            continue
        if abs(float(e) - aerodrome_elev) > 100.0:
            report.warn("elevation", f.key,
                        f"threshold elevation {float(e):.0f} M differs from aerodrome "
                        f"elevation {aerodrome_elev:.0f} M by more than 100 M")

    # magnetic variation vs published true/magnetic bearings
    mag = ahp.text("magneticVariation")
    if mag is not None:
        implied = []
        for f in features:
            if f.type != "RunwayDirection":
                continue
            tb, mb = f.text("trueBearing"), f.text("magneticBearing")
            if tb and mb:
                implied.append((f.text("designator"), float(tb) - float(mb)))
        if implied:
            avg = sum(d for _, d in implied) / len(implied)
            if abs(avg - float(mag)) > 1.5:
                report.warn("magnetic-variation", ahp.key,
                            f"aerodrome magneticVariation={mag} DEG but the published true vs "
                            f"magnetic runway bearings imply ~{avg:+.1f} DEG "
                            f"(samples: {', '.join(f'{d}:{v:+.0f}' for d, v in implied)}). "
                            "Confirm against the AIP and WMM date.")
            else:
                report.info("magnetic-variation", ahp.key,
                            f"magneticVariation={mag} DEG consistent with published bearings "
                            f"(implied {avg:+.1f} DEG).")

    # runways within a sane distance of the ARP
    if arp:
        for f in features:
            if f.type != "Runway":
                continue
            el = next((e for e in features if e.key == f.key + "_ELEMENT"), None)
            if el is None:
                continue
            surf = surface_of(el.timeslice, "extent")
            ring = poslist_ring(surf) if surf is not None else []
            if not ring:
                continue
            dmin = min(haversine(arp[0], arp[1], la, lo) for la, lo in ring)
            if dmin > 5000:
                report.warn("extent", f.key,
                            f"runway pavement is {dmin / 1000:.1f} km from the ARP")
            report.info("extent", f.key, f"nearest pavement {dmin:.0f} m from the ARP")


def check_surfaces(features: list[Feature], report: Report) -> None:
    for f in features:
        if f.type not in ("TaxiwayElement", "ApronElement", "RunwayElement"):
            continue
        surf = surface_of(f.timeslice, "extent")
        if surf is None:
            report.error("surface", f.key, "no extent surface")
            continue
        ring = poslist_ring(surf)
        problems = []
        if len(ring) < 4:
            problems.append(f"only {len(ring)} distinct vertices")
        area = ring_area_m2(ring)
        if area <= 0:
            problems.append("zero area")
        # closed ring in the GML
        pl = surf.find(f".//{GML}posList")
        nums = [float(x) for x in (pl.text or "").split()]
        pts = list(zip(nums[0::2], nums[1::2]))
        if not pts or pts[0] != pts[-1]:
            problems.append("exterior ring is not closed")
        if problems:
            report.error("surface", f.key, "; ".join(problems))
        else:
            report.info("surface", f.key, f"valid closed ring, area {area:,.0f} m2")

        # declared width vs modelled width for taxiways
        if f.type == "TaxiwayElement":
            declared = f.timeslice.findtext(AIXM + "width")
            declared_m = float(declared) if declared else None
            if declared_m and area > 0:
                # approximate mean width from area / length
                length = f.timeslice.findtext(AIXM + "length")
                if length:
                    modelled = area / float(length)
                    if abs(modelled - declared_m) > declared_m * 0.25:
                        report.warn("surface-width", f.key,
                                    f"modelled mean width {modelled:.1f} M vs declared "
                                    f"{declared_m:.0f} M (>25 % difference)")


def check_navaids(features: list[Feature], report: Report) -> None:
    for f in features:
        if f.type == "Navaid":
            ntype = f.text("type") or "?"
            if ntype in ("VOR_DME", "VOR", "DME") and f.node("location") is None:
                report.error("navaid", f.key, f"{ntype} has no location")
            if ntype.startswith("NDB"):
                freq = f.timeslice.findtext(f"{AIXM}frequency")
                if freq:
                    khz = float(freq) * 1000.0
                    if not (190.0 <= khz <= 1750.0):
                        report.warn("navaid", f.key,
                                    f"NDB frequency {khz:.0f} kHz outside the 190-1750 kHz band")
            refs = [c.find(AIXM + "theNavaidEquipment") for c in f.nodes("navaidEquipment")]
            if not refs:
                report.warn("navaid", f.key, "composite navaid has no equipment components")
        if f.type == "DME":
            chan = f.text("channel")
            if chan and not re.fullmatch(r"\d{2,3}[XY]", chan):
                report.error("navaid", f.key, f"DME channel '{chan}' is not of the form 122X")


def check_provenance(features: list[Feature], manifest: dict | None, report: Report) -> None:
    if not manifest:
        report.warn("provenance", "-", "no build_manifest.json: provenance not checked")
        return
    derived = [r for r in manifest["records"] if r["confidence"] != "published"]
    report.info("provenance", "-",
                f"{len(manifest['records'])} objects: "
                f"{len(manifest['records']) - len(derived)} published, {len(derived)} derived/assumed")

    # every feature carrying a non-published provenance must be annotated
    annotated = set()
    for f in features:
        for ann in f.nodes("annotation"):
            txt = "".join(ann.itertext())
            if "Provenance:" in txt:
                annotated.add(f.key)
    missing = []
    for rec in derived:
        # inline properties have no time slice of their own; the parent feature
        # carries the annotation instead
        if rec.get("inline"):
            continue
        if rec["key"] and rec["key"] not in annotated:
            missing.append(f"{rec['aixm_object']} {rec['label']}")
    report.structural_check("provenance annotation present", missing, len(derived))
    for m_ in missing:
        report.warn("provenance", m_, "object with derived/assumed provenance has no "
                                      "provenance annotation")


def check_frequencies(features: list[Feature], report: Report) -> None:
    seen: dict[str, list[str]] = {}
    for f in features:
        if f.type != "RadioCommunicationChannel":
            continue
        freq = f.timeslice.findtext(f"{AIXM}frequencyTransmission")
        if not freq:
            continue
        if float(freq) <= 0 or float(freq) > 140.0:
            report.error("frequency", f.key, f"implausible VHF frequency {freq} MHZ")
        seen.setdefault(freq, []).append(f.key)
    # duplicate frequencies across separate channel objects are legal (different
    # services) but worth surfacing when they are the same service
    report.info("frequency", "-",
                f"{sum(len(v) for v in seen.values())} channels over {len(seen)} distinct frequencies: "
                + ", ".join(sorted(seen)))


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #
def render_report(xml_path: Path, report: Report, schema_ok: bool,
                  features: list[Feature], manifest: dict | None,
                  crosscheck: dict | None) -> str:
    sha = hashlib.sha256(xml_path.read_bytes()).hexdigest()[:16]
    # Deliberately no wall-clock timestamp: a deterministic report lets CI tell
    # "out/ is stale" from "out/ was regenerated a minute later".
    effective = (manifest or {}).get("dataset", {}).get("effective", "unknown")
    n_err, n_warn, n_info = report.count("ERROR"), report.count("WARN"), report.count("INFO")

    L: list[str] = []
    L.append("# HUEN AIXM 5.1.1 - validation report\n")
    L.append(f"Produced by `scripts/validate_aixm.py` for data set effective "
             f"`{effective}`.\n")
    L.append("| item | value |")
    L.append("|---|---|")
    L.append(f"| data set | `{xml_path.name}` |")
    L.append(f"| sha256 (first 16) | `{sha}` |")
    L.append(f"| schema | AIXM 5.1.1 BasicMessage (`schema/aixm-5.1.1/`) |")
    L.append(f"| AIXM objects (time slices) | {len(features)} |")
    L.append(f"| schema validation | **{'PASS' if schema_ok else 'FAIL'}** |")
    L.append(f"| findings | {n_err} error, {n_warn} warning, {n_info} info |")
    L.append("")

    if manifest:
        L.append("## 1. Feature inventory\n")
        L.append("| AIXM feature | objects |")
        L.append("|---|---|")
        counts: dict[str, int] = {}
        for f in features:
            counts[f.type] = counts.get(f.type, 0) + 1
        for k in sorted(counts):
            L.append(f"| {k} | {counts[k]} |")
        L.append("")

    L.append("## 2. Structural checks\n")
    L.append("| check | result | checked | failures |")
    L.append("|---|---|---|---|")
    for name, status, checked, failed in report.structural:
        L.append(f"| {name} | {'PASS' if status == 'PASS' else '**FAIL**'} | {checked} | {failed} |")
    L.append("")

    L.append("## 3. Findings\n")
    if not report.findings:
        L.append("No findings.\n")
    else:
        L.append("| severity | check | object | detail |")
        L.append("|---|---|---|---|")
        order = {"ERROR": 0, "WARN": 1, "INFO": 2}
        for f in sorted(report.findings, key=lambda x: (order[x.severity], x.check)):
            detail = f.message.replace("|", "\\|")
            L.append(f"| {f.severity} | {f.check} | `{f.object}` | {detail} |")
        L.append("")
        L.append("Grading: **ERROR** = the data set is internally inconsistent and must be "
                 "fixed; **WARN** = the sources conflict or the value needs the AIP to "
                 "confirm; **INFO** = informational.\n")

    if manifest:
        L.append("## 4. Provenance\n")
        L.append("| confidence | objects |")
        L.append("|---|---|")
        conf: dict[str, int] = {}
        for r in manifest["records"]:
            conf[r["confidence"]] = conf.get(r["confidence"], 0) + 1
        for k in sorted(conf):
            L.append(f"| {k} | {conf[k]} |")
        L.append("")
        nonpub = [r for r in manifest["records"] if r["confidence"] != "published"]
        if nonpub:
            L.append("Objects that are **not** published values:\n")
            L.append("| AIXM object | label | confidence | source | note |")
            L.append("|---|---|---|---|---|")
            for r in nonpub:
                note = "; ".join(n for n in r["notes"] if n)[:180].replace("|", "\\|")
                L.append(f"| {r['aixm_object']} | {r['label']} | {r['confidence']} | "
                         f"{r['source']} | {note} |")
            L.append("")

        L.append("## 5. Sources used\n")
        L.append("| id | source | retrieved |")
        L.append("|---|---|---|")
        for sid, s in manifest.get("sources", {}).items():
            L.append(f"| `{sid}` | {s.get('name', '')} | {s.get('retrieved', '')} |")
        L.append("")

    if crosscheck:
        L.append("## 6. Independent cross-check against flight tracks\n")
        L.append(crosscheck.get("summary_markdown", "_no summary available_"))
        L.append("")

    L.append("## 7. Not modelled / known gaps\n")
    L.append("The schema-valid message is complete for the classes it claims, but the "
             "following AD 2 content is absent because no accessible source states it. "
             "Each is a candidate for the next iteration:\n")
    L.append("- **ILS/approach aids**: DAFIF reports ILS on 17; ident, frequency and "
             "glidepath geometry were not available, so no `Localizer`/`Glidepath` "
             "objects are encoded.")
    L.append("- **Obstacles** (`VerticalStructure`) and **terrain**: no open obstacle "
             "survey was reachable from the build environment.")
    L.append("- **Declared distances for reduced/intersection take-off positions**: only "
             "full-length values are published in the sources used.")
    L.append("- **Taxiway designators, holding positions, markings and lighting**: the "
             "derived taxiway network is geometric only; `TaxiHoldingPosition` and "
             "related features are omitted rather than invented.")
    L.append("- **Operating hours as a timesheet**: AIXM 5.1.1 `AirportHeliportAvailability` "
             "has no time interval, so H24 is carried as an annotated statement.")
    L.append("- **Aircraft stands** (`AircraftStand`): stand designators and positions are "
             "not published in the sources used.")
    L.append("")
    L.append("---")
    L.append("")
    L.append("*This data set is not official AIS data and must not be used for "
             "navigation, flight planning or operational purposes. The Uganda CAA AIP "
             "(AD 2 HUEN) is the authoritative source.*")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--xml", default=str(ROOT_DIR / "out" / "HUEN_AIP_DS.xml"))
    ap.add_argument("--report", default=str(ROOT_DIR / "out" / "HUEN_validation_report.md"))
    args = ap.parse_args()

    xml_path = Path(args.xml)
    if not xml_path.exists():
        print(f"missing {xml_path}; run scripts/build_aixm.py first")
        return 2

    report = Report()
    schema_ok = check_schema(xml_path, report)

    doc = etree.parse(str(xml_path))
    root = doc.getroot()
    features = parse_features(root)

    check_structure(features, root, report)
    check_aerodrome(features, report)
    check_runways(features, report)
    check_surfaces(features, report)
    check_navaids(features, report)
    check_frequencies(features, report)

    manifest_path = xml_path.parent / "build_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    check_provenance(features, manifest, report)

    crosscheck_path = xml_path.parent / "track_crosscheck.json"
    crosscheck = json.loads(crosscheck_path.read_text()) if crosscheck_path.exists() else None

    text = render_report(xml_path, report, schema_ok, features, manifest, crosscheck)
    Path(args.report).write_text(text, encoding="utf-8")

    print(f"schema: {'PASS' if schema_ok else 'FAIL'}")
    print(f"objects: {len(features)}")
    print(f"findings: {report.count('ERROR')} error, {report.count('WARN')} warning, "
          f"{report.count('INFO')} info")
    print(f"report: {args.report}")

    # console digest of ERROR/WARN
    for f in report.findings:
        if f.severity in ("ERROR", "WARN"):
            print(f"  {f.severity}: [{f.check}] {f.object} - {f.message[:160]}")

    return 0 if (schema_ok and report.count("ERROR") == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
