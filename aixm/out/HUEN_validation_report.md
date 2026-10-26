# HUEN AIXM 5.1.1 - validation report

Produced by `scripts/validate_aixm.py` for data set effective `2026-10-26T00:00:00Z`.

| item | value |
|---|---|
| data set | `HUEN_AIP_DS.xml` |
| sha256 (first 16) | `d0f9158635c6e116` |
| schema | AIXM 5.1.1 BasicMessage (`schema/aixm-5.1.1/`) |
| AIXM objects (time slices) | 40 |
| schema validation | **PASS** |
| findings | 0 error, 2 warning, 23 info |

## 1. Feature inventory

| AIXM feature | objects |
|---|---|
| AirTrafficControlService | 2 |
| AirportHeliport | 1 |
| Apron | 2 |
| ApronElement | 2 |
| DME | 1 |
| FireFightingService | 1 |
| NDB | 2 |
| Navaid | 1 |
| OrganisationAuthority | 1 |
| RadioCommunicationChannel | 4 |
| Runway | 2 |
| RunwayCentrelinePoint | 4 |
| RunwayDirection | 4 |
| RunwayElement | 2 |
| Taxiway | 4 |
| TaxiwayElement | 4 |
| Unit | 2 |
| VOR | 1 |

## 2. Structural checks

| check | result | checked | failures |
|---|---|---|---|
| unique gml:id values | PASS | 336 | 0 |
| xlink references resolve | PASS | 40 | 0 |
| BASELINE time slices | PASS | 40 | 0 |
| single time slice per feature | PASS | 40 | 0 |
| provenance annotation present | PASS | 21 | 0 |

## 3. Findings

| severity | check | object | detail |
|---|---|---|---|
| WARN | declared-distance | `RCP_12_30_12` | RWY 12: LDA 1748 M is 652 M shorter than TORA 2400 M, but no displaced threshold (DISTHR) is encoded. Either the threshold is displaced or the published LDA needs confirming against AIP AD 2.13. |
| WARN | runway-length | `RWY_12_30` | 12/30: geodesic distance between threshold coordinates is 2446.3 M but nominalLength is 2408 M (difference +38.3 M > 30 M tolerance). Published end coordinates are rounded to ~11 m in the source. |
| INFO | aerodrome | `AHP_HUEN` | controlType JOINT (civil/military joint use). |
| INFO | bearing | `RCP_17_35_17` | RWY 17: published 171.0 DEG vs geodesic 170.96 DEG (+0.04 DEG). |
| INFO | bearing | `RCP_17_35_35` | RWY 35: published 351.0 DEG vs geodesic 350.96 DEG (+0.04 DEG). |
| INFO | bearing | `RCP_12_30_12` | RWY 12: published 121.0 DEG vs geodesic 121.28 DEG (-0.28 DEG). |
| INFO | bearing | `RCP_12_30_30` | RWY 30: published 301.0 DEG vs geodesic 301.28 DEG (-0.28 DEG). |
| INFO | extent | `RWY_17_35` | nearest pavement 1687 m from the ARP |
| INFO | extent | `RWY_12_30` | nearest pavement 1086 m from the ARP |
| INFO | frequency | `-` | 4 channels over 4 distinct frequencies: 118.100, 119.100, 126.600, 128.500 |
| INFO | magnetic-variation | `AHP_HUEN` | magneticVariation=1.0 DEG consistent with published bearings (implied +2.0 DEG). |
| INFO | pavement | `RWY_17_35_ELEMENT` | runway pavement area 168,307 m2, 4 vertices |
| INFO | pavement | `RWY_12_30_ELEMENT` | runway pavement area 112,445 m2, 4 vertices |
| INFO | provenance | `-` | 56 objects: 35 published, 21 derived/assumed |
| INFO | reciprocal | `RWY_17_35` | 17/35: reciprocal bearings consistent (+0.00 DEG). |
| INFO | reciprocal | `RWY_12_30` | 12/30: reciprocal bearings consistent (+0.00 DEG). |
| INFO | runway-length | `RWY_17_35` | 17/35: geodesic 3679.9 M vs nominal 3658 M (+21.9 M) - within tolerance. |
| INFO | surface | `RWY_17_35_ELEMENT` | valid closed ring, area 168,307 m2 |
| INFO | surface | `RWY_12_30_ELEMENT` | valid closed ring, area 112,445 m2 |
| INFO | surface | `TWY_PARALLEL_ELEMENT` | valid closed ring, area 82,313 m2 |
| INFO | surface | `TWY_LINK_NORTH_ELEMENT` | valid closed ring, area 3,221 m2 |
| INFO | surface | `TWY_LINK_SOUTH_ELEMENT` | valid closed ring, area 2,759 m2 |
| INFO | surface | `TWY_LINK_APRON_ELEMENT` | valid closed ring, area 8,741 m2 |
| INFO | surface | `APRON_MAIN_ELEMENT` | valid closed ring, area 310,854 m2 |
| INFO | surface | `APRON_NORTH_ELEMENT` | valid closed ring, area 75,608 m2 |

Grading: **ERROR** = the data set is internally inconsistent and must be fixed; **WARN** = the sources conflict or the value needs the AIP to confirm; **INFO** = informational.

## 4. Provenance

| confidence | objects |
|---|---|
| assumed | 7 |
| derived | 14 |
| published | 35 |

Objects that are **not** published values:

| AIXM object | label | confidence | source | note |
|---|---|---|---|---|
| RunwayElement | 17/35 | derived | ourairports |  |
| RunwayElement | 12/30 | derived | ourairports |  |
| RunwayDeclaredDistance | 12 LDA | assumed | chart | LDA 1748 m vs TORA 2400 m with no published displaced threshold. |
| DME | NN | assumed | ourairports |  |
| RadioCommunicationChannel | 119.100 MHZ | assumed | dafif | DAFIF lists APP 119.1 as 'opr unrel'; may be stale or an alternate sector. |
| Unit | EBBTWR | assumed | ourairports |  |
| Unit | EBBAPP | assumed | ourairports |  |
| AirTrafficControlService | TWR | assumed | ourairports |  |
| AirTrafficControlService | APP | assumed | ourairports |  |
| Taxiway | East-side parallel taxiway; 180 m from the 17/35 centreline, spanning lat 0.0235 | derived | tracks |  |
| TaxiwayElement | PARALLEL | derived | tracks |  |
| Taxiway | Link between the runway 17 end area and the parallel taxiway. | derived | tracks |  |
| TaxiwayElement | LINK_NORTH | derived | tracks |  |
| Taxiway | Link between the runway 35 end area and the parallel taxiway. | derived | tracks |  |
| TaxiwayElement | LINK_SOUTH | derived | tracks |  |
| Taxiway | Link between the main apron and the parallel taxiway. | derived | tracks |  |
| TaxiwayElement | LINK_APRON | derived | tracks |  |
| Apron | MAIN APRON | derived | tracks |  |
| ApronElement | MAIN APRON | derived | tracks |  |
| Apron | NORTH APRON | derived | tracks |  |
| ApronElement | NORTH APRON | derived | tracks |  |

## 5. Sources used

| id | source | retrieved |
|---|---|---|
| `ourairports` | OurAirports open data set (airports/runways/navaids/frequencies) | 2026-10-26 |
| `dafif` | DAFIF figures as republished (Airports Worldwide, SkyVector) | 2026-10-26 |
| `chart` | HUEN aerodrome/AD 2 chart extract (declared distances, RFF, hours) | 2026-10-26 |
| `tracks` | FR24 position tracks in this repository (tracks/, 350 flights) | 2026-10-26 |
| `wmm` | World Magnetic Model estimate for the ARP | 2026-10-26 |

## 6. Independent cross-check against flight tracks

`tracks/` holds **350** flights; **332** contain ground-level positions at Entebbe (altitude < 60 ft within 25 km of the ARP). Those positions were compared with the geometry in the AIXM message.

### 6.1 Runway centreline agreement

| runway | modelled length (M) | ground points on centreline | median offset | p90 offset |
|---|---|---|---|---|
| 17/35 | 3680 | 3026 | 14.9 m | 26.7 m |
| 12/30 | 2446 | 301 | 15.0 m | 26.9 m |

A median offset of a few metres means the published threshold coordinates and the runway bearings in the data set describe the pavement aircraft actually use. Offsets are lateral distance to the modelled centreline, counted only for points within 30 m of it.

### 6.2 Runway and direction in use

| usage | movements |
|---|---|
| ARR RWY 17/35 dir 17 | 146 |
| DEP RWY 17/35 dir 17 | 143 |
| ARR RWY 17/35 dir 35 | 20 |
| DEP RWY 17/35 dir 35 | 16 |

### 6.3 Departure line-up and arrival touchdown vs modelled thresholds

| runway direction | phase | n | median distance from THR | p90 |
|---|---|---|---|---|
| 17 | departure line-up | 143 | 381 m | 528 m |
| 35 | departure line-up | 16 | 16 m | 29 m |
| 17 | first ground point (arrival) | 146 | 1955 m | 2342 m |
| 35 | first ground point (arrival) | 20 | 1846 m | 2219 m |

A departure line-up close to the threshold corroborates the threshold position; a larger value indicates an intersection departure. The arrival figure is the first *reported* ground position, which FR24 playback samples coarsely, so it is an upper bound on touchdown distance rather than a touchdown measurement.

Runway 12/30 never appears as the runway in use in any analysed track, although 100 flights taxi close enough to its centreline to register points on it. Combined with the aerodrome chart legend, that suggests 12/30 is not in routine use; confirm against the AIP.

### 6.4 Coverage of the derived apron/taxiway surfaces

| classification of ground points | count | share |
|---|---|---|
| apron | 5710 | 35.9% |
| uncovered | 4170 | 26.2% |
| on_runway | 3327 | 20.9% |
| taxiway | 2698 | 17.0% |

**26.2%** of observed ground movement falls outside every modelled surface. That is the honest measure of how incomplete the derived taxiway and apron network is: aircraft also use links, holding bays and stand taxilanes that no accessible source describes.


## 7. Not modelled / known gaps

The schema-valid message is complete for the classes it claims, but the following AD 2 content is absent because no accessible source states it. Each is a candidate for the next iteration:

- **ILS/approach aids**: DAFIF reports ILS on 17; ident, frequency and glidepath geometry were not available, so no `Localizer`/`Glidepath` objects are encoded.
- **Obstacles** (`VerticalStructure`) and **terrain**: no open obstacle survey was reachable from the build environment.
- **Declared distances for reduced/intersection take-off positions**: only full-length values are published in the sources used.
- **Taxiway designators, holding positions, markings and lighting**: the derived taxiway network is geometric only; `TaxiHoldingPosition` and related features are omitted rather than invented.
- **Operating hours as a timesheet**: AIXM 5.1.1 `AirportHeliportAvailability` has no time interval, so H24 is carried as an annotated statement.
- **Aircraft stands** (`AircraftStand`): stand designators and positions are not published in the sources used.

---

*This data set is not official AIS data and must not be used for navigation, flight planning or operational purposes. The Uganda CAA AIP (AD 2 HUEN) is the authoritative source.*
