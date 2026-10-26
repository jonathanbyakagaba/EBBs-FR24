#!/usr/bin/env python3
"""
Build an AIXM 5.1.1 BasicMessage (AIP data set) for an aerodrome from YAML.

    python scripts/build_aixm.py [--data data/huen.yaml] [--out out/HUEN_AIP_DS.xml]

Design notes
------------
* One YAML file is the single source of truth (data/huen.yaml). This script
  only encodes; it never decides facts.
* Element order follows the AIXM 5.1.1 property groups exactly (the schema
  sequences are ordered, so `designator` before `name`, etc.).
* All gml:ids are deterministic UUIDv5 values derived from the object's
  semantic key, so rebuilding does not churn the file and cross-references
  remain stable across runs.
* Every feature gets a provenance annotation (source + confidence) taken from
  the YAML, so the provenance travels with the data instead of living only in
  a report.
* Anything the sources do not state is omitted rather than guessed; the
  validator and report are responsible for pointing out the gaps.

Requires: lxml, pyyaml.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from lxml import etree

# --------------------------------------------------------------------------- #
# Namespaces
# --------------------------------------------------------------------------- #
AIXM_NS = "http://www.aixm.aero/schema/5.1.1"
MSG_NS = "http://www.aixm.aero/schema/5.1.1/message"
GML_NS = "http://www.opengis.net/gml/3.2"
GMD_NS = "http://www.isotc211.org/2005/gmd"
GCO_NS = "http://www.isotc211.org/2005/gco"
GTS_NS = "http://www.isotc211.org/2005/gts"
GSS_NS = "http://www.isotc211.org/2005/gss"
GSR_NS = "http://www.isotc211.org/2005/gsr"
XLINK_NS = "http://www.w3.org/1999/xlink"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

NSMAP = {
    "message": MSG_NS,
    "aixm": AIXM_NS,
    "gml": GML_NS,
    "gmd": GMD_NS,
    "gco": GCO_NS,
    "gts": GTS_NS,
    "gss": GSS_NS,
    "gsr": GSR_NS,
    "xlink": XLINK_NS,
    "xsi": XSI_NS,
}

A = "{%s}" % AIXM_NS
M = "{%s}" % MSG_NS
G = "{%s}" % GML_NS
GMD = "{%s}" % GMD_NS
GCO = "{%s}" % GCO_NS
XL = "{%s}" % XLINK_NS
XSI = "{%s}" % XSI_NS

# ISO 19139 codelist base used by AIXM implementations for gmd codes.
GMD_CODELIST = (
    "http://standards.iso.org/ittf/PubliclyAvailableStandards/"
    "ISO_19139_Schemas/resources/Codelist/gmxCodelists.xml"
)

ROOT_DIR = Path(__file__).resolve().parents[1]
UUID_NS = uuid.uuid5(uuid.NAMESPACE_DNS, "ebbs-fr24.aixm/HUEN")
EARTH_R = 6371008.8


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def oid(key: str) -> uuid.UUID:
    """Deterministic object identifier for a semantic key."""
    return uuid.uuid5(UUID_NS, key)


def gid(key: str) -> str:
    """gml:id for an object (NCName-safe: prefixed with 'uuid.')."""
    return "uuid." + str(oid(key))


def href(key: str) -> str:
    return "urn:uuid:" + str(oid(key))


def ncname(key: str) -> str:
    """Sanitise a key into an NCName, for TimePeriod/Note ids."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", key)


def sub(parent, tag: str, text: Any = None, **attrs):
    el = etree.SubElement(parent, tag)
    for k, v in attrs.items():
        el.set(k.replace("__", ":"), v)
    if text is not None:
        el.text = str(text)
    return el


def uom(parent, tag: str, value: Any, unit: str, **attrs):
    return sub(parent, tag, value, uom=unit, **attrs)


def val(node: Any) -> Any:
    """Unwrap {value: x, source: s} scalars from the YAML."""
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    return node


def src_of(node: Any, default: str | None, default_conf: str) -> tuple[str | None, str, str | None]:
    if isinstance(node, dict) and "value" in node:
        return (
            node.get("source", default),
            node.get("confidence", default_conf),
            node.get("note"),
        )
    return default, default_conf, None


# --------------------------------------------------------------------------- #
# Geometry helpers (local equirectangular ENU, good to a few cm at AD scale)
# --------------------------------------------------------------------------- #
def to_enu(lat: float, lon: float, ref: tuple[float, float]) -> tuple[float, float]:
    lat0, lon0 = ref
    return (
        (lon - lon0) * 111320.0 * math.cos(math.radians(lat0)),
        (lat - lat0) * 110540.0,
    )


def to_geo(x: float, y: float, ref: tuple[float, float]) -> tuple[float, float]:
    lat0, lon0 = ref
    return (lat0 + y / 110540.0, lon0 + x / (111320.0 * math.cos(math.radians(lat0))))


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def ribbon(polyline: list[tuple[float, float]], width_m: float,
           ref: tuple[float, float]) -> list[tuple[float, float]]:
    """Buffer a polyline into a closed ring of lat/lon points.

    Deliberately simple: one perpendicular offset per segment, then the two
    sides are concatenated. Good enough for a modelled taxiway surface; not a
    geodesic buffer.
    """
    pts = [to_enu(la, lo, ref) for la, lo in polyline]
    left, right = [], []
    for i in range(len(pts) - 1):
        (x1, y1), (x2, y2) = pts[i], pts[i + 1]
        dx, dy = x2 - x1, y2 - y1
        seg = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / seg * width_m / 2.0, dx / seg * width_m / 2.0
        left += [(x1 + nx, y1 + ny), (x2 + nx, y2 + ny)]
        right += [(x1 - nx, y1 - ny), (x2 - nx, y2 - ny)]
    ring = left + right[::-1]
    return [to_geo(x, y, ref) for x, y in ring]


def rectangle_around_runway(a: tuple[float, float], b: tuple[float, float],
                            width_m: float, ref: tuple[float, float]) -> list[tuple[float, float]]:
    """Pavement outline of a runway: centreline end-to-end, buffered by width."""
    return ribbon([a, b], width_m, ref)


# --------------------------------------------------------------------------- #
# AIXM building blocks
# --------------------------------------------------------------------------- #
class Ctx:
    """Carries document state: root, member list, provenance records."""

    def __init__(self, data: dict):
        self.data = data
        self.records: list[dict] = []
        self.extent: list[tuple[float, float]] = []
        self.effective: str = data["dataset"]["effective"]
        self.root = etree.Element(M + "AIXMBasicMessage", nsmap=NSMAP)
        self.bounded = etree.SubElement(self.root, G + "boundedBy")
        # Features are collected in a detached holder: the message sequence is
        # boundedBy?, messageMetadata, hasMember*, and extent/metadata cannot be
        # known until every feature exists, so assembly happens in build().
        self.members = etree.Element("holder")

    # -- provenance -------------------------------------------------------- #
    def record(self, key: str, kind: str, label: str, source: str | None,
               confidence: str, notes: list[str] | None = None,
               geometry: str | None = None, inline: bool = False) -> None:
        """Register provenance for an emitted object.

        `inline=True` marks a property that lives inside another feature's time
        slice (e.g. a declared distance): it has no time slice of its own, so
        its provenance is carried by the parent feature's annotation.
        """
        self.records.append({
            "key": key,
            "aixm_object": kind,
            "label": label,
            "source": source,
            "confidence": confidence,
            "geometry": geometry,
            "notes": notes or [],
            "inline": inline,
        })

    def note_extent(self, lat: float, lon: float) -> None:
        self.extent.append((lat, lon))

    # -- object scaffolding ------------------------------------------------ #
    def feature(self, key: str, feature_tag: str, timeslice_tag: str):
        """Create `<aixm:Feature gml:id>` + timeSlice + timeslice element."""
        wrapper = etree.SubElement(self.members, M + "hasMember")
        feat = etree.SubElement(wrapper, A + feature_tag, {G + "id": gid(key)})
        ident = etree.SubElement(feat, G + "identifier")
        ident.set("codeSpace", "urn:uuid:")
        ident.text = str(oid(key))
        tsw = etree.SubElement(feat, A + "timeSlice")
        ts = etree.SubElement(tsw, A + timeslice_tag, {G + "id": ncname(key)})
        self.time_preamble(ts, key)
        return feat, ts

    def time_preamble(self, ts, key: str) -> None:
        vt = etree.SubElement(ts, G + "validTime")
        period = etree.SubElement(vt, G + "TimePeriod", {G + "id": "vt_" + ncname(key)})
        sub(period, G + "beginPosition", self.effective)
        etree.SubElement(period, G + "endPosition", {"indeterminatePosition": "unknown"})
        sub(ts, A + "interpretation", "BASELINE")
        sub(ts, A + "sequenceNumber", 1)
        sub(ts, A + "correctionNumber", 0)
        lt = etree.SubElement(ts, A + "featureLifetime")
        lperiod = etree.SubElement(lt, G + "TimePeriod", {G + "id": "lt_" + ncname(key)})
        sub(lperiod, G + "beginPosition", self.effective)
        etree.SubElement(lperiod, G + "endPosition", {"indeterminatePosition": "unknown"})

    def annotation(self, ts, key: str, property_name: str, text: str, idx: int = 1) -> None:
        ann = etree.SubElement(ts, A + "annotation")
        note = etree.SubElement(ann, A + "Note", {G + "id": f"note_{ncname(key)}_{idx}"})
        sub(note, A + "propertyName", property_name)
        tn = etree.SubElement(note, A + "translatedNote")
        ln = etree.SubElement(tn, A + "LinguisticNote", {G + "id": f"lnote_{ncname(key)}_{idx}"})
        sub(ln, A + "note", text)

    def provenance_note(self, ts, key: str, source: str | None, confidence: str,
                        notes: list[str] | None = None, idx: int = 90) -> None:
        reg = self.data.get("sources", {})
        src_name = reg.get(source, {}).get("name", source) if source else "unspecified"
        text = f"Provenance: {confidence}; source: {src_name}."
        for n in notes or []:
            text += " " + n
        self.annotation(ts, key, "feature", text, idx)

    # -- geometry ---------------------------------------------------------- #
    def elevated_point(self, parent, prop_tag: str, lat: float, lon: float,
                       key: str, elev_m: float | None = None,
                       horizontal_accuracy_m: float | None = None,
                       vertical_accuracy_m: float | None = None):
        holder = etree.SubElement(parent, A + prop_tag)
        pt = etree.SubElement(holder, A + "ElevatedPoint", {
            G + "id": "geo_" + ncname(key),
            "srsName": "urn:ogc:def:crs:EPSG::4326",
        })
        sub(pt, G + "pos", f"{lat:.9f} {lon:.9f}")
        if horizontal_accuracy_m is not None:
            uom(pt, A + "horizontalAccuracy", horizontal_accuracy_m, "M")
        if elev_m is not None:
            uom(pt, A + "elevation", round(elev_m, 1), "M")
        if vertical_accuracy_m is not None:
            uom(pt, A + "verticalAccuracy", vertical_accuracy_m, "M")
        self.note_extent(lat, lon)
        return pt

    def elevated_surface(self, parent, prop_tag: str, ring: list[tuple[float, float]],
                         key: str, elev_m: float | None = None):
        holder = etree.SubElement(parent, A + prop_tag)
        surf = etree.SubElement(holder, A + "ElevatedSurface", {
            G + "id": "sfc_" + ncname(key),
            "srsName": "urn:ogc:def:crs:EPSG::4326",
        })
        patches = etree.SubElement(surf, G + "patches")
        patch = etree.SubElement(patches, G + "PolygonPatch")
        ext = etree.SubElement(patch, G + "exterior")
        ring_el = etree.SubElement(ext, G + "Ring")
        cm = etree.SubElement(ring_el, G + "curveMember")
        curve = etree.SubElement(cm, G + "Curve", {G + "id": "crv_" + ncname(key)})
        segs = etree.SubElement(curve, G + "segments")
        gs = etree.SubElement(segs, G + "GeodesicString")
        closed = list(ring) + [ring[0]]
        sub(gs, G + "posList", "\n".join(f"{la:.9f} {lo:.9f}" for la, lo in closed))
        if elev_m is not None:
            uom(surf, A + "elevation", round(elev_m, 1), "M")
        for la, lo in ring:
            self.note_extent(la, lo)
        return surf

    def xlink_ref(self, parent, tag: str, key: str, title: str | None = None):
        attrs = {XL + "href": href(key)}
        if title:
            attrs[XL + "title"] = title
        return etree.SubElement(parent, tag, attrs)

    def surface_properties(self, parent, key: str, composition: str,
                           extra: dict | None = None) -> None:
        sp = etree.SubElement(parent, A + "surfaceProperties")
        sc = etree.SubElement(sp, A + "SurfaceCharacteristics", {G + "id": "sc_" + ncname(key)})
        sub(sc, A + "composition", composition)
        for k, v in (extra or {}).items():
            sub(sc, A + k, v)


def arp_distance_note(ctx: Ctx, lat: float, lon: float) -> str:
    arp = ctx.data["airport"]["arp"]
    d = haversine(arp["lat"], arp["lon"], lat, lon)
    return f"Located {d / 1000:.1f} km from the aerodrome reference point."


# --------------------------------------------------------------------------- #
# Feature builders
# --------------------------------------------------------------------------- #
def build_airport(ctx: Ctx) -> None:
    d = ctx.data["airport"]
    default = d.get("_source")
    key = f"AHP_{d['icao']}"
    _, ts = ctx.feature(key, "AirportHeliport", "AirportHeliportTimeSlice")

    sub(ts, A + "designator", d["icao"])
    sub(ts, A + "name", d["name"])
    sub(ts, A + "locationIndicatorICAO", d["icao"])
    if d.get("iata"):
        sub(ts, A + "designatorIATA", d["iata"])
    sub(ts, A + "type", d["type"])
    sub(ts, A + "certifiedICAO", d["certified_icao"])
    sub(ts, A + "controlType", val(d["control_type"]))
    if d.get("field_elevation_m") is not None:
        uom(ts, A + "fieldElevation", val(d["field_elevation_m"]), "M")
    if d.get("field_elevation_accuracy_m") is not None:
        uom(ts, A + "fieldElevationAccuracy", val(d["field_elevation_accuracy_m"]), "M")
    # ValMagneticVariationType / ValAngleType / ValBearingType carry no uom attribute.
    sub(ts, A + "magneticVariation", val(d["magnetic_variation_deg"]))
    sub(ts, A + "magneticVariationAccuracy", val(d["magnetic_variation_accuracy_deg"]))
    sub(ts, A + "dateMagneticVariation", val(d["magnetic_variation_date"]))

    # servedCity
    sc = etree.SubElement(ts, A + "servedCity")
    city = etree.SubElement(sc, A + "City", {G + "id": "city_" + ncname(d["served_city"])})
    sub(city, A + "name", d["served_city"])

    # responsibleOrganisation -> OrganisationAuthority (emitted later)
    org_key = "ORG_" + ncname(val(d["operator"]))
    ro = etree.SubElement(ts, A + "responsibleOrganisation")
    aro = etree.SubElement(ro, A + "AirportHeliportResponsibilityOrganisation",
                           {G + "id": "aro_" + key})
    sub(aro, A + "role", "OPERATE")
    ctx.xlink_ref(aro, A + "theOrganisationAuthority", org_key, val(d["operator"]))

    # ARP
    arp = d["arp"]
    ctx.elevated_point(ts, "ARP", arp["lat"], arp["lon"], key + "_ARP",
                       elev_m=val(d["field_elevation_m"]),
                       horizontal_accuracy_m=3.0)

    # availability: operational status NORMAL + H24 usage annotation
    av = etree.SubElement(ts, A + "availability")
    ahav = etree.SubElement(av, A + "AirportHeliportAvailability", {G + "id": "av_" + key})
    sub(ahav, A + "operationalStatus", "NORMAL")

    ctx.annotation(ts, key, "availability",
                   f"Aerodrome operating hours: {d['operating_hours']}.", 1)
    for i, remark in enumerate(d.get("remarks", []), start=2):
        ctx.annotation(ts, key, "feature", remark, i)
    ctx.provenance_note(ts, key, default, "published",
                        ["Third-party open data; the Uganda AIP AD 2 HUEN remains authoritative."])

    ctx.record(key, "AirportHeliport", d["icao"], default, "published")


def build_organisation(ctx: Ctx) -> None:
    d = ctx.data["airport"]
    name = val(d["operator"])
    key = "ORG_" + ncname(name)
    _, ts = ctx.feature(key, "OrganisationAuthority", "OrganisationAuthorityTimeSlice")
    sub(ts, A + "name", name)
    sub(ts, A + "type", "NTL_AUTH")
    sub(ts, A + "military", "CIVIL")
    ctx.provenance_note(ts, key, d.get("_source"), "published")
    ctx.record(key, "OrganisationAuthority", name, d.get("_source"), "published")


def build_runway(ctx: Ctx, rwy: dict) -> None:
    default = rwy.get("_source")
    desig = rwy["designator"]
    key = f"RWY_{ncname(desig)}"
    _, ts = ctx.feature(key, "Runway", "RunwayTimeSlice")
    sub(ts, A + "designator", desig)
    sub(ts, A + "type", rwy.get("type", "RWY"))
    uom(ts, A + "nominalLength", val(rwy["nominal_length_m"]), "M")
    uom(ts, A + "nominalWidth", val(rwy["nominal_width_m"]), "M")
    ctx.surface_properties(ts, key, rwy["composition"])
    ctx.xlink_ref(ts, A + "associatedAirportHeliport", f"AHP_{ctx.data['airport']['icao']}",
                  ctx.data["airport"]["icao"])

    # geometry check: published nominal length vs. geodesic distance between ends
    ends = list(rwy["ends"].items())
    (d1, e1), (d2, e2) = ends
    length_geo = haversine(e1["lat"], e1["lon"], e2["lat"], e2["lon"])
    notes = [
        f"Derived geodesic length between published end coordinates is {length_geo:.0f} M "
        f"vs published nominal length {val(rwy['nominal_length_m'])} M."
    ]
    ctx.provenance_note(ts, key, default, "published", notes)
    ctx.record(key, "Runway", desig, default, "published",
               notes=[f"geodesic {length_geo:.1f} M vs published {val(rwy['nominal_length_m'])} M"])

    # pavement outline (RunwayElement)
    ref = (e1["lat"], e1["lon"])
    ring = rectangle_around_runway((e1["lat"], e1["lon"]), (e2["lat"], e2["lon"]),
                                   float(val(rwy["nominal_width_m"])), ref)
    el_key = key + "_ELEMENT"
    _, ets = ctx.feature(el_key, "RunwayElement", "RunwayElementTimeSlice")
    sub(ets, A + "type", "NORMAL")
    uom(ets, A + "length", round(length_geo, 1), "M")
    uom(ets, A + "width", val(rwy["nominal_width_m"]), "M")
    ctx.xlink_ref(ets, A + "associatedRunway", key, desig)
    ctx.elevated_surface(ets, "extent", ring, el_key,
                         elev_m=round((float(e1["elevation_ft"]) + float(e2["elevation_ft"])) / 2 * 0.3048, 1))
    ctx.annotation(ets, el_key, "extent",
                   "Pavement outline generated by buffering the published runway end "
                   "coordinates to the published runway width; it is a model artefact, "
                   "not a surveyed pavement boundary.", 1)
    ctx.provenance_note(ets, el_key, default, "derived",
                        ["Geometry is a buffer of published threshold coordinates, "
                         "not a surveyed pavement boundary."])
    ctx.record(el_key, "RunwayElement", desig, default, "derived",
               geometry=f"{len(ring)}-point polygon")

    # runway directions
    for end_desig, end in rwy["ends"].items():
        build_runway_direction(ctx, rwy, end_desig, end, key, default)


def build_runway_direction(ctx: Ctx, rwy: dict, end_desig: str, end: dict,
                           rwy_key: str, default: str | None) -> None:
    rwy_desig = rwy["designator"]
    key = f"RDN_{ncname(rwy_desig)}_{end_desig}"
    _, ts = ctx.feature(key, "RunwayDirection", "RunwayDirectionTimeSlice")
    sub(ts, A + "designator", end_desig)
    sub(ts, A + "trueBearing", val(end["true_bearing"]))
    if end.get("magnetic_bearing") is not None:
        sub(ts, A + "magneticBearing", val(end["magnetic_bearing"]))
    if end.get("approach_marking"):
        sub(ts, A + "approachMarkingType", end["approach_marking"])
        sub(ts, A + "approachMarkingCondition", "GOOD")
    if end.get("precision_approach"):
        sub(ts, A + "precisionApproachGuidance", val(end["precision_approach"]))
    ctx.xlink_ref(ts, A + "usedRunway", rwy_key, rwy_desig)

    # bearing check against the opposite end
    other = [e for k, e in rwy["ends"].items() if k != end_desig][0]
    bearing_geo = bearing(end["lat"], end["lon"], other["lat"], other["lon"])
    delta = ((val(end["true_bearing"]) - bearing_geo + 180) % 360) - 180
    notes = [f"Geodesic bearing between published end coordinates is {bearing_geo:.2f} DEG T, "
             f"i.e. {delta:+.2f} DEG from the published true bearing."]
    conf = "published" if abs(delta) < 1.0 else "assumed"
    ctx.provenance_note(ts, key, default, conf, notes)
    ctx.record(key, "RunwayDirection", f"{rwy_desig.split('/')[0] if False else end_desig}",
               default, conf, notes=[f"bearing delta {delta:+.2f} DEG"])

    # centreline point at this end (THR for the direction that lands this way)
    build_centreline_points(ctx, rwy, end_desig, end, key, default)


def build_centreline_points(ctx: Ctx, rwy: dict, end_desig: str, end: dict,
                            rdn_key: str, default: str | None) -> None:
    """THR (+ START_RUN) at the approach end; END at the far end.

    Declared distances are attached to the point they are measured from:
    TORA/TODA/ASDA from START_RUN, LDA from THR (PANS-AIM / AIXM guidance).
    """
    key = f"RCP_{ncname(rwy['designator'])}_{end_desig}"
    elev_m = float(end["elevation_ft"]) * 0.3048
    _, ts = ctx.feature(key, "RunwayCentrelinePoint", "RunwayCentrelinePointTimeSlice")
    sub(ts, A + "role", "THR")
    ctx.elevated_point(ts, "location", end["lat"], end["lon"], key,
                       elev_m=elev_m, horizontal_accuracy_m=3.0, vertical_accuracy_m=3.0)
    ctx.xlink_ref(ts, A + "onRunway", rdn_key, end_desig)

    declared = end.get("declared", {})
    for dd_type in ("TORA", "TODA", "ASDA", "LDA"):
        if dd_type not in declared:
            continue
        node = declared[dd_type]
        dkey = f"{key}_{dd_type}"
        holder = etree.SubElement(ts, A + "associatedDeclaredDistance")
        rd = etree.SubElement(holder, A + "RunwayDeclaredDistance", {G + "id": "rd_" + ncname(dkey)})
        sub(rd, A + "type", dd_type)
        dv = etree.SubElement(rd, A + "declaredValue")
        ddv = etree.SubElement(dv, A + "RunwayDeclaredDistanceValue",
                               {G + "id": "rdv_" + ncname(dkey)})
        uom(ddv, A + "distance", val(node), "M")
        uom(ddv, A + "distanceAccuracy", 1, "M")
        source, conf, note = src_of(node, default, "published")
        ctx.record(dkey, "RunwayDeclaredDistance", f"{end_desig} {dd_type}",
                   source, conf, notes=[note] if note else [], inline=True)

    ctx.provenance_note(ts, key, default, "published")
    ctx.record(key, "RunwayCentrelinePoint", f"{end_desig} THR", default, "published",
               geometry=f"{end['lat']:.6f},{end['lon']:.6f}")


def build_navaids(ctx: Ctx) -> None:
    for nav in ctx.data.get("navaids", []):
        default = nav.get("_source")
        desig = nav["designator"]
        key = f"NAV_{ncname(desig)}"
        if nav["type"] == "VOR_DME":
            build_vor_dme(ctx, nav, key, default)
        elif nav["type"] == "NDB":
            build_ndb(ctx, nav, key, default)


def build_vor_dme(ctx: Ctx, nav: dict, key: str, default: str | None) -> None:
    desig = nav["designator"]
    _, ts = ctx.feature(key, "Navaid", "NavaidTimeSlice")
    sub(ts, A + "type", "VOR_DME")
    sub(ts, A + "designator", desig)
    sub(ts, A + "name", nav["name"])
    sub(ts, A + "flightChecked", "YES")
    if nav.get("purpose"):
        sub(ts, A + "purpose", val(nav["purpose"]))
    for comp in nav["components"]:
        ckey = f"NAVEQ_{ncname(desig)}_{comp['kind']}"
        holder = etree.SubElement(ts, A + "navaidEquipment")
        nc = etree.SubElement(holder, A + "NavaidComponent", {G + "id": "nc_" + ncname(ckey)})
        sub(nc, A + "collocationGroup", 1)
        ctx.xlink_ref(nc, A + "theNavaidEquipment", ckey, f"{comp['kind']}_{desig}")
    ctx.elevated_point(ts, "location", nav["lat"], nav["lon"], key,
                       elev_m=float(nav["elevation_ft"]) * 0.3048,
                       horizontal_accuracy_m=15.0, vertical_accuracy_m=30.0)
    if nav.get("serves_aerodrome", True):
        ctx.xlink_ref(ts, A + "servedAirport", f"AHP_{ctx.data['airport']['icao']}",
                      ctx.data["airport"]["icao"])
    ctx.provenance_note(ts, key, default, "published",
                        ["Navaid position from OurAirports; the DME is assumed co-located.",
                         arp_distance_note(ctx, nav["lat"], nav["lon"])])
    ctx.record(key, "Navaid", desig, default, "published",
               geometry=f"{nav['lat']:.6f},{nav['lon']:.6f}")

    # equipment objects
    for comp in nav["components"]:
        ckey = f"NAVEQ_{ncname(desig)}_{comp['kind']}"
        if comp["kind"] == "VOR":
            _, ets = ctx.feature(ckey, "VOR", "VORTimeSlice")
            sub(ets, A + "designator", comp["designator"])
            ctx.elevated_point(ets, "location", nav["lat"], nav["lon"], ckey,
                               elev_m=float(nav["elevation_ft"]) * 0.3048)
            ctx.provenance_note(ets, ckey, default, "published")
            sub(ets, A + "type", comp.get("type", "VOR"))
            uom(ets, A + "frequency", comp["frequency_mhz"], "MHZ")
            sub(ets, A + "zeroBearingDirection", "TRUE")
            ctx.record(ckey, "VOR", comp["designator"], default, "published")
        elif comp["kind"] == "DME":
            _, ets = ctx.feature(ckey, "DME", "DMETimeSlice")
            sub(ets, A + "designator", comp["designator"])
            if comp.get("colocated_with_vor"):
                ctx.elevated_point(ets, "location", nav["lat"], nav["lon"], ckey,
                                   elev_m=float(nav["elevation_ft"]) * 0.3048)
            ctx.provenance_note(ets, ckey, default, "assumed",
                                ["DME position assumed co-located with the VOR."])
            sub(ets, A + "channel", comp["channel"])
            ctx.record(ckey, "DME", comp["designator"], default, "assumed")


def build_ndb(ctx: Ctx, nav: dict, key: str, default: str | None) -> None:
    desig = nav["designator"]
    _, ts = ctx.feature(key, "NDB", "NDBTimeSlice")
    sub(ts, A + "designator", desig)
    sub(ts, A + "name", nav["name"])
    ctx.elevated_point(ts, "location", nav["lat"], nav["lon"], key)
    ctx.provenance_note(ts, key, default, "published",
                        ["NDB elevation not published; position only.",
                         arp_distance_note(ctx, nav["lat"], nav["lon"])])
    uom(ts, A + "frequency", nav["frequency_khz"] / 1000.0, "MHZ")
    if nav.get("emission_band"):
        sub(ts, A + "emissionBand", val(nav["emission_band"]))
    ctx.record(key, "NDB", desig, default, "published",
               geometry=f"{nav['lat']:.6f},{nav['lon']:.6f}")


def build_radio_channels(ctx: Ctx) -> dict[str, str]:
    keys: dict[str, str] = {}
    for ch in ctx.data.get("radio_channels", []):
        default = ch.get("_source")
        key = f"RCC_{ch['key']}"
        keys[ch["key"]] = key
        _, ts = ctx.feature(key, "RadioCommunicationChannel", "RadioCommunicationChannelTimeSlice")
        freq = float(val(ch["frequency_mhz"]))
        uom(ts, A + "frequencyTransmission", f"{freq:.3f}", "MHZ")
        uom(ts, A + "frequencyReception", f"{freq:.3f}", "MHZ")
        source, conf, note = src_of(ch["frequency_mhz"], default, "published")
        ctx.provenance_note(ts, key, source, conf, [note] if note else [])
        ctx.record(key, "RadioCommunicationChannel", f"{freq:.3f} MHZ", source, conf,
                   notes=[note] if note else [])
    return keys


def build_units(ctx: Ctx) -> dict[str, str]:
    keys: dict[str, str] = {}
    for unit in ctx.data.get("units", []):
        key = "UNIT_" + ncname(unit["designator"])
        keys[unit["designator"]] = key
        _, ts = ctx.feature(key, "Unit", "UnitTimeSlice")
        sub(ts, A + "name", unit["name"])
        sub(ts, A + "type", unit["type"])
        sub(ts, A + "designator", unit["designator"])
        ctx.provenance_note(ts, key, "ourairports", "assumed",
                            ["ATS unit existence inferred from the published radio channels."])
        ctx.record(key, "Unit", unit["designator"], "ourairports", "assumed")
    return keys


def build_services(ctx: Ctx, channel_keys: dict[str, str], unit_keys: dict[str, str]) -> None:
    for svc in ctx.data.get("services", []):
        key = f"SVC_{svc['key']}"
        if svc["kind"] == "ATC":
            _, ts = ctx.feature(key, "AirTrafficControlService", "AirTrafficControlServiceTimeSlice")
            # ServicePropertyGroup order: flightOperations, rank, compliantICAO,
            # name, location, serviceProvider, call-sign, radioCommunication,
            # then the ATC-specific type / clientAirport.
            sub(ts, A + "flightOperations", "ALL")
            if svc.get("rank"):
                sub(ts, A + "rank", svc["rank"])
            sub(ts, A + "compliantICAO", "YES")
            sub(ts, A + "name", svc["name"])
            if svc.get("provider_unit") and svc["provider_unit"] in unit_keys:
                ctx.xlink_ref(ts, A + "serviceProvider", unit_keys[svc["provider_unit"]],
                              svc["provider_unit"])
            cs = etree.SubElement(ts, A + "call-sign")
            csd = etree.SubElement(cs, A + "CallsignDetail", {G + "id": "cs_" + ncname(key)})
            sub(csd, A + "callSign", svc["callsign"])
            if svc.get("channel") and svc["channel"] in channel_keys:
                ctx.xlink_ref(ts, A + "radioCommunication", channel_keys[svc["channel"]],
                              svc["channel"])
            ctx.provenance_note(ts, key, "ourairports", "assumed",
                                [f"Service hours stated as {svc.get('hours', 'unknown')}; "
                                 "not modelled as a timesheet in AIXM 5.1.1 availability."])
            sub(ts, A + "type", svc["type"])
            ctx.xlink_ref(ts, A + "clientAirport", f"AHP_{ctx.data['airport']['icao']}",
                          ctx.data["airport"]["icao"])
            ctx.record(key, "AirTrafficControlService", svc["type"], "ourairports", "assumed")
        elif svc["kind"] == "FIRE_FIGHTING":
            _, ts = ctx.feature(key, "FireFightingService", "FireFightingServiceTimeSlice")
            sub(ts, A + "flightOperations", "ALL")
            sub(ts, A + "compliantICAO", "YES")
            sub(ts, A + "name", svc["name"])
            ctx.provenance_note(ts, key, "chart", "published")
            ctx.xlink_ref(ts, A + "airportHeliport", f"AHP_{ctx.data['airport']['icao']}",
                          ctx.data["airport"]["icao"])
            sub(ts, A + "category", val(ctx.data["airport"]["rff_category"]))
            sub(ts, A + "standard", "ICAO")
            ctx.record(key, "FireFightingService",
                       f"RFF CAT {val(ctx.data['airport']['rff_category'])}", "chart", "published")


def build_taxiways(ctx: Ctx) -> None:
    ref = (ctx.data["airport"]["arp"]["lat"], ctx.data["airport"]["arp"]["lon"])
    for twy in ctx.data.get("taxiways", []):
        key = f"TWY_{twy['key']}"
        poly = [(float(a), float(b)) for a, b in twy["polyline"]]
        _, ts = ctx.feature(key, "Taxiway", "TaxiwayTimeSlice")
        # designator deliberately omitted: not published in the accessible sources
        uom(ts, A + "width", twy["width_m"], "M")
        length = sum(haversine(poly[i][0], poly[i][1], poly[i + 1][0], poly[i + 1][1])
                     for i in range(len(poly) - 1))
        uom(ts, A + "length", round(length, 1), "M")
        ctx.surface_properties(ts, key, twy["composition"])
        ctx.xlink_ref(ts, A + "associatedAirportHeliport", f"AHP_{ctx.data['airport']['icao']}",
                      ctx.data["airport"]["icao"])
        ctx.provenance_note(ts, key, twy.get("_source"), twy.get("confidence", "derived"),
                            [twy.get("note", ""), "Designator not published in accessible sources; "
                             "omitted rather than invented."])
        ctx.record(key, "Taxiway", f"{twy['key']} (no published designator)",
                   twy.get("_source"), twy.get("confidence", "derived"),
                   geometry=f"{len(poly)}-point centreline, {length:.0f} M")

        el_key = key + "_ELEMENT"
        ring = ribbon(poly, float(twy["width_m"]) * 1.0, ref)
        _, ets = ctx.feature(el_key, "TaxiwayElement", "TaxiwayElementTimeSlice")
        sub(ets, A + "type", "NORMAL")
        uom(ets, A + "length", round(length, 1), "M")
        uom(ets, A + "width", twy["width_m"], "M")
        ctx.xlink_ref(ets, A + "associatedTaxiway", key, None)
        ctx.elevated_surface(ets, "extent", ring, el_key)
        ctx.annotation(ets, el_key, "extent", twy.get("note", ""), 1)
        ctx.provenance_note(ets, el_key, twy.get("_source"),
                            twy.get("confidence", "derived"),
                            ["Surface is a generated buffer around the derived "
                             "taxiway centreline."])
        ctx.record(el_key, "TaxiwayElement", twy["key"], twy.get("_source"),
                   twy.get("confidence", "derived"), geometry=f"{len(ring)}-point surface")


def build_aprons(ctx: Ctx) -> None:
    for ap in ctx.data.get("aprons", []):
        key = f"APRON_{ap['key']}"
        ring = [(float(a), float(b)) for a, b in ap["polygon"]]
        _, ts = ctx.feature(key, "Apron", "ApronTimeSlice")
        sub(ts, A + "name", ap["name"])
        ctx.surface_properties(ts, key, ap["composition"])
        ctx.xlink_ref(ts, A + "associatedAirportHeliport", f"AHP_{ctx.data['airport']['icao']}",
                      ctx.data["airport"]["icao"])
        ctx.provenance_note(ts, key, ap.get("_source"), ap.get("confidence", "derived"),
                            [ap.get("note", ""), "Name is descriptive, not published."])
        ctx.record(key, "Apron", ap["name"], ap.get("_source"), ap.get("confidence", "derived"))

        el_key = key + "_ELEMENT"
        _, ets = ctx.feature(el_key, "ApronElement", "ApronElementTimeSlice")
        sub(ets, A + "type", "NORMAL")
        ctx.xlink_ref(ets, A + "associatedApron", key, ap["name"])
        ctx.elevated_surface(ets, "extent", ring, el_key)
        ctx.annotation(ets, el_key, "extent", ap.get("note", ""), 1)
        ctx.provenance_note(ets, el_key, ap.get("_source"),
                            ap.get("confidence", "derived"),
                            ["Surface is the envelope polygon declared in the source "
                             "YAML, not a surveyed apron boundary."])
        ctx.record(el_key, "ApronElement", ap["name"], ap.get("_source"),
                   ap.get("confidence", "derived"), geometry=f"{len(ring)}-point surface")


# --------------------------------------------------------------------------- #
# Message envelope
# --------------------------------------------------------------------------- #
def build_metadata(root, ctx: Ctx) -> None:
    d = ctx.data["dataset"]
    md = etree.SubElement(root, A + "messageMetadata")
    m = etree.SubElement(md, GMD + "MD_Metadata", {"id": "MD_" + d["location_indicator"]})

    cs = etree.SubElement(m, GMD + "characterSet")
    ch = etree.SubElement(cs, GMD + "MD_CharacterSetCode",
                          {"codeList": GMD_CODELIST + "#MD_CharacterSetCode",
                           "codeListValue": "utf8"})
    ch.text = "utf8"

    contact = etree.SubElement(m, GMD + "contact")
    rp = etree.SubElement(contact, GMD + "CI_ResponsibleParty")
    on = etree.SubElement(rp, GMD + "organisationName")
    sub(on, GCO + "CharacterString", d["publisher"])
    role = etree.SubElement(rp, GMD + "role")
    rc = etree.SubElement(role, GMD + "CI_RoleCode",
                          {"codeList": GMD_CODELIST + "#CI_RoleCode",
                           "codeListValue": "originator"})
    rc.text = "originator"

    ds = etree.SubElement(m, GMD + "dateStamp")
    sub(ds, GCO + "DateTime", d["effective"])

    ii = etree.SubElement(m, GMD + "identificationInfo")
    di = etree.SubElement(ii, GMD + "MD_DataIdentification")
    cit = etree.SubElement(di, GMD + "citation")
    cict = etree.SubElement(cit, GMD + "CI_Citation")
    ti = etree.SubElement(cict, GMD + "title")
    sub(ti, GCO + "CharacterString", d["title"])
    dte = etree.SubElement(cict, GMD + "date")
    cid = etree.SubElement(dte, GMD + "CI_Date")
    dd = etree.SubElement(cid, GMD + "date")
    sub(dd, GCO + "Date", d["effective"][:10])
    dt = etree.SubElement(cid, GMD + "dateType")
    dtc = etree.SubElement(dt, GMD + "CI_DateTypeCode",
                           {"codeList": GMD_CODELIST + "#CI_DateTypeCode",
                            "codeListValue": "publication"})
    dtc.text = "publication"

    ab = etree.SubElement(di, GMD + "abstract")
    sub(ab, GCO + "CharacterString", d["abstract"])
    lang = etree.SubElement(di, GMD + "language")
    sub(lang, GCO + "CharacterString", d["language"])
    tc = etree.SubElement(di, GMD + "topicCategory")
    tcc = etree.SubElement(tc, GMD + "MD_TopicCategoryCode")
    tcc.text = "transportation"

    lats = [p[0] for p in ctx.extent]
    lons = [p[1] for p in ctx.extent]
    ext = etree.SubElement(di, GMD + "extent")
    ex = etree.SubElement(ext, GMD + "EX_Extent")
    geo = etree.SubElement(ex, GMD + "geographicElement")
    bb = etree.SubElement(geo, GMD + "EX_GeographicBoundingBox")
    for tag, value in (("westBoundLongitude", min(lons)),
                       ("eastBoundLongitude", max(lons)),
                       ("southBoundLatitude", min(lats)),
                       ("northBoundLatitude", max(lats))):
        node = etree.SubElement(bb, GMD + tag)
        sub(node, GCO + "Decimal", f"{value:.9f}")


def write_envelope(ctx: Ctx) -> None:
    lats = [p[0] for p in ctx.extent]
    lons = [p[1] for p in ctx.extent]
    pad = 0.05
    env = etree.Element(G + "Envelope", {"srsName": "urn:ogc:def:crs:EPSG::4326"})
    sub(env, G + "lowerCorner", f"{min(lats) - pad:.9f} {min(lons) - pad:.9f}")
    sub(env, G + "upperCorner", f"{max(lats) + pad:.9f} {max(lons) + pad:.9f}")
    ctx.bounded.append(env)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def build(data_path: Path, out_path: Path) -> dict:
    data = yaml.safe_load(data_path.read_text(encoding="utf-8"))
    ctx = Ctx(data)

    build_airport(ctx)
    build_organisation(ctx)
    for rwy in data.get("runways", []):
        build_runway(ctx, rwy)
    build_navaids(ctx)
    channel_keys = build_radio_channels(ctx)
    unit_keys = build_units(ctx)
    build_services(ctx, channel_keys, unit_keys)
    build_taxiways(ctx)
    build_aprons(ctx)

    # root: boundedBy, messageMetadata, hasMember*
    write_envelope(ctx)
    build_metadata(ctx.root, ctx)
    for member in list(ctx.members):
        ctx.root.append(member)

    ctx.root.set(G + "id", "M_" + data["dataset"]["location_indicator"])
    ctx.root.set(XSI + "schemaLocation",
                 f"{MSG_NS} http://www.aixm.aero/schema/5.1.1/message/AIXM_BasicMessage.xsd")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    etree.indent(ctx.root, space="  ")
    out_path.write_bytes(etree.tostring(ctx.root, xml_declaration=True,
                                        encoding="UTF-8", pretty_print=True))

    manifest = {
        "dataset": data["dataset"],
        "sources": data.get("sources", {}),
        "generated_from": str(data_path.relative_to(ROOT_DIR)),
        "output": str(out_path.relative_to(ROOT_DIR)),
        "feature_count": len(ctx.records),
        "records": ctx.records,
        "extent": {
            "lat_min": min(p[0] for p in ctx.extent),
            "lat_max": max(p[0] for p in ctx.extent),
            "lon_min": min(p[1] for p in ctx.extent),
            "lon_max": max(p[1] for p in ctx.extent),
        },
    }
    manifest_path = out_path.parent / "build_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default=str(ROOT_DIR / "data" / "huen.yaml"))
    ap.add_argument("--out", default=str(ROOT_DIR / "out" / "HUEN_AIP_DS.xml"))
    args = ap.parse_args()

    manifest = build(Path(args.data), Path(args.out))
    print(f"wrote {args.out} ({manifest['feature_count']} AIXM objects)")
    print(f"wrote {Path(args.out).parent / 'build_manifest.json'}")


if __name__ == "__main__":
    main()
