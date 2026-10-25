"""Enumerate EVERY carrier at Entebbe (EBB) from FR24's airport board — v2.

v1 found nothing because it assumed where FR24 nests the flight list, and
it died with HTTP 400 on days older than a couple. v2:

  * --probe       asks the board how far back it will answer (1..10 days)
  *              finds the flight list wherever it actually is, rather than
                 trusting a hard-coded path
  *              accepts IATA *or* ICAO airport codes (the board sometimes
                 omits one of them)
  *              prints the response skeleton when it reads a day but can
                 extract no flights, so the next fix is obvious

Usage:
    python scripts/enumerate_airport_v2.py --probe
    python scripts/enumerate_airport_v2.py            # today only
    python scripts/enumerate_airport_v2.py --since 2026-10-24
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fr24.authentication import login
from fr24.clients.curl import CurlAsyncClient
from fr24.json import AirportListParams, airport_list, get_json_headers

sys.path.insert(0, str(Path(__file__).parent))
from fr24_auth import read_subscription_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EBB_CODES = {"HUEN", "EBB"}
EAT = timezone(timedelta(hours=3))

ICAO2IATA = {
    "HUEN": "EBB", "HKJK": "NBO", "HTDA": "DAR", "FAOR": "JNB",
    "EGKK": "LGW", "EGGW": "LTN", "EGLL": "LHR", "DNMM": "LOS",
    "VABB": "BOM", "HKMO": "MBA", "HCMM": "MGQ", "HJJJ": "JUB",
    "HSSJ": "JUB", "FVRG": "HRE", "FVHA": "HRE", "HTZA": "ZNZ",
    "HTKJ": "JRO", "HBBA": "BJM", "FLKK": "LUN", "HAAB": "ADD",
    "OTHH": "DOH", "OMDB": "DXB", "OMAA": "AUH", "OMSJ": "SHJ",
    "LTFM": "IST", "EBBR": "BRU", "HECA": "CAI", "HRZA": "KGL",
    "EHAM": "AMS", "FQMA": "MPM", "FMMI": "TNR", "FBSK": "GBE",
    "FYWH": "WDH", "FACT": "CPT", "FALE": "DUR", "FNLU": "LAD",
    "DGAA": "ACC", "DIAP": "ABJ", "GOOY": "DSS", "FLSN": "NLA",
    "HLLM": "MJI", "HKKI": "KIS", "HTMW": "MWZ",
}

FIELDS = ["flight_id_hex", "number", "callsign", "airline", "direction",
          "from_iata", "to_iata", "registration", "aircraft", "status",
          "atod_utc", "atoa_utc", "stod_utc", "stoa_utc", "ebb_date"]


def fmt(ts) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def codes(detail) -> set[str]:
    c = ((detail or {}).get("code") or {})
    return {str(c[k]).upper() for k in ("icao", "iata") if c.get(k)}


def is_ebb(detail) -> bool:
    return bool(codes(detail) & EBB_CODES)


def code_or_blank(detail) -> str:
    c = ((detail or {}).get("code") or {})
    return c.get("icao") or c.get("iata") or ""


def iata(detail) -> str:
    c = ((detail or {}).get("code") or {})
    raw = c.get("icao") or c.get("iata") or ""
    return ICAO2IATA.get(raw, c.get("iata") or raw)


def find_flight_lists(obj, path="") -> list[tuple[str, dict]]:
    """Every dict in the payload whose 'data' is a list of flight items."""
    found: list[tuple[str, dict]] = []
    if isinstance(obj, dict):
        d = obj.get("data")
        if isinstance(d, list) and d and isinstance(d[0], dict) and "identification" in d[0]:
            found.append((path or "data", obj))
        for k, v in obj.items():
            found.extend(find_flight_lists(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(find_flight_lists(v, f"{path}[{i}]"))
    return found


def skeleton(obj, depth=4, width=6):
    """Key-only outline of a payload, for when we find nothing."""
    if isinstance(obj, dict):
        return {k: skeleton(v, depth - 1, width) for k, v in list(obj.items())[:width]} if depth > 0 else "{...}"
    if isinstance(obj, list):
        return [skeleton(obj[0], depth - 1, width), f"...{len(obj)} items"] if obj and depth > 0 else []
    return type(obj).__name__


def row_from_item(item: dict, airline_fallback: str = "") -> dict | None:
    ident = item.get("identification") or {}
    fid = ident.get("id")
    times = item.get("time") or {}
    real = times.get("real") or {}
    sched = times.get("scheduled") or {}
    atod, atoa = real.get("departure"), real.get("arrival")
    origin, dest = item.get("airport", {}).get("origin"), item.get("airport", {}).get("destination")
    if not fid:
        return None
    at_ebb_origin, at_ebb_dest = is_ebb(origin), is_ebb(dest)
    if not (at_ebb_origin or at_ebb_dest):
        return None
    if atod is None and atoa is None:
        return None                       # never operated
    direction = "DEP" if at_ebb_origin else "ARR"
    event = atod if direction == "DEP" else (atoa or atod)
    if event is None:
        return None
    return {
        "flight_id_hex": f"{int(fid, 16):08x}",
        "number": ((ident.get("number") or {}).get("default") or ""),
        "callsign": ident.get("callsign") or "",
        "airline": (item.get("airline") or {}).get("name") or airline_fallback,
        "direction": direction,
        "from_iata": iata(origin),
        "to_iata": iata(dest),
        "registration": (item.get("aircraft") or {}).get("registration") or "",
        "aircraft": ((item.get("aircraft") or {}).get("model") or {}).get("code") or "",
        "status": (item.get("status") or {}).get("text") or "",
        "atod_utc": fmt(atod), "atoa_utc": fmt(atoa),
        "stod_utc": fmt(sched.get("departure")), "stoa_utc": fmt(sched.get("arrival")),
        "ebb_date": datetime.fromtimestamp(int(event), EAT).strftime("%Y-%m-%d"),
    }


async def board_page(client, headers, auth, mode: str, ts: int, page: int):
    """One page of the EBB board. Returns (raw_items, more, payload)."""
    resp = await airport_list(
        client,
        AirportListParams(airport="EBB", mode=mode, page=page, limit=100, timestamp=ts),
        headers=headers, auth=auth,
    )
    raw = resp.json()
    lists = find_flight_lists(raw)
    if not lists:
        return [], False, raw
    _, node = lists[0]
    return node.get("data") or [], bool((node.get("page") or {}).get("more")), raw


async def probe(client, headers, auth) -> None:
    print("probing how far back the EBB board will answer (arrivals only):")
    for off in (0, 1, 2, 3, 4, 5, 7, 10):
        day = datetime.now(EAT).date() - timedelta(days=off)
        ts = int(datetime(day.year, day.month, day.day, 23, 59, tzinfo=EAT).timestamp())
        try:
            items, more, _ = await board_page(client, headers, auth, "arrivals", ts, 1)
            print(f"  {day} (-{off}d): OK   {len(items)} items, more={more}")
        except Exception as e:
            print(f"  {day} (-{off}d): {type(e).__name__}: {e}")
        await asyncio.sleep(2)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="default: today only")
    ap.add_argument("--out", default=str(ROOT / "data" / "movements.csv"))
    ap.add_argument("--probe", action="store_true", help="report the board's reachable depth")
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=2.0)
    args = ap.parse_args()

    headers = get_json_headers()
    movements: dict[str, dict] = {}
    out_path = Path(args.out)
    if out_path.exists():
        for r in csv.DictReader(open(out_path, newline="")):
            if r.get("flight_id_hex"):
                movements[r["flight_id_hex"].strip().lower()] = r
    print(f"{len(movements)} existing movements")

    async with CurlAsyncClient() as client:
        auth = await login(client, creds={"subscriptionKey": read_subscription_key(), "token": None})
        print("auth:", auth.get("message"))

        if args.probe:
            await probe(client, headers, auth)
            print()

        today = datetime.now(EAT).date()
        start = date.fromisoformat(args.since) if args.since else today
        days = [start + timedelta(days=i) for i in range(max((today - start).days, 0) + 1)]

        for day in days:
            ts = int(datetime(day.year, day.month, day.day, 23, 59, tzinfo=EAT).timestamp())
            day_added = 0
            for mode in ("arrivals", "departures"):
                page, empty_payload = 1, None
                while page <= args.max_pages:
                    try:
                        items, more, raw = await board_page(client, headers, auth, mode, ts, page)
                    except Exception as e:
                        print(f"{day} {mode} p{page}: FAILED {type(e).__name__}: {e}")
                        break
                    if not items:
                        empty_payload = raw if empty_payload is None else empty_payload
                        break
                    for item in items:
                        row = row_from_item(item)
                        if row and row["flight_id_hex"] not in movements:
                            movements[row["flight_id_hex"]] = row
                            day_added += 1
                    if not more:
                        break
                    page += 1
                    await asyncio.sleep(args.sleep)
                if empty_payload is not None:
                    print(f"{day} {mode}: read OK but 0 flights parsed — payload shape:")
                    print(json.dumps(skeleton(empty_payload), indent=1)[:900])
                await asyncio.sleep(args.sleep)
            carriers = sorted({r["airline"] for r in movements.values() if r["ebb_date"] == str(day)})
            print(f"{day}: +{day_added} (total {len(movements)}, {len(carriers)} carriers) "
                  f"{','.join(carriers[:6])}{'…' if len(carriers) > 6 else ''}")

    rows = sorted(movements.values(), key=lambda r: (r["ebb_date"], r["atod_utc"]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} movements -> {out_path}")

    have = set()
    for folder in (ROOT / "tracks", ROOT / "tracks_out"):
        if folder.exists():
            for fp in folder.rglob("*.csv"):
                m = re.search(r"-([0-9a-f]{8})$", fp.stem)
                if m:
                    have.add(m.group(1).lower())
    targets = [r for r in rows if r["flight_id_hex"] not in have]
    tp = ROOT / "data" / "track_targets.csv"
    with open(tp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["flight_id_hex", "number", "callsign", "date", "from", "to"])
        w.writeheader()
        for r in targets:
            w.writerow({"flight_id_hex": r["flight_id_hex"], "number": r["number"],
                        "callsign": r["callsign"], "date": r["ebb_date"],
                        "from": r["from_iata"], "to": r["to_iata"]})
    print(f"wrote {len(targets)} track targets -> {tp} ({len(have)} already downloaded)")


if __name__ == "__main__":
    asyncio.run(main())
