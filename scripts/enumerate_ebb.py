"""Enumerate Entebbe (HUEN/EBB) arrivals & departures from FR24 history.

For every flight number in flight_numbers_seed.csv, page FR24's flight
list backwards from now to --since, keep only legs touching HUEN
(Entebbe), and merge them into data/movements.csv (one row per flight).
Also writes data/track_targets.csv: every enumerated flight whose track
has not been downloaded yet, ready for scripts/fetch_tracks.py.

This is the same verified machinery as the FR24 repo's gap recovery
(flight_list by flight number + playback), pointed at an airport
instead of one airline. Everything is included: no cherry-picking.
The list is append/update — safe to re-run; rows are keyed by FR24
flight id, and a flight found under two numbers (codeshares) is kept
once.

Run on the user's Windows machine (the sandbox cannot reach FR24):
    pip install "fr24[curl,polars]==0.4.0" tzdata
    python scripts/enumerate_ebb.py --since 2026-01-01

Note: flight-list history depth and playback-track retention are
separate things. The list reaching January does NOT guarantee FR24
still serves position tracks for January flights — test track
downloads on a handful of old flights before queueing thousands
(see README "Step 0").
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fr24.authentication import login
from fr24.clients.curl import CurlAsyncClient
from fr24.json import FlightListParams, flight_list, flight_list_df, get_json_headers

sys.path.insert(0, str(Path(__file__).parent))
from fr24_auth import read_subscription_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HUEN = "HUEN"
EAT = timezone(timedelta(hours=3))  # Entebbe local (no DST)

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
    "HLLM": "MJI", "HKKI": "KIS", "HTMW": "MWZ", "HUENX": "EBB",
}

FIELDS = ["flight_id_hex", "number", "callsign", "airline", "direction",
          "from_iata", "to_iata", "registration", "aircraft", "status",
          "atod_utc", "atoa_utc", "stod_utc", "stoa_utc", "ebb_date"]


def fmt(ts) -> str:
    if ts is None:
        return ""
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_movements(path: Path) -> dict:
    rows = {}
    if path.exists():
        for r in csv.DictReader(open(path, newline="")):
            rows[r["flight_id_hex"].strip().lower()] = r
    return rows


def downloaded_ids() -> set:
    ids = set()
    for folder in (ROOT / "tracks", ROOT / "tracks_out"):
        if folder.exists():
            for fp in folder.rglob("*.csv"):
                m = re.search(r"-([0-9a-f]{8})$", fp.stem)
                if m:
                    ids.add(m.group(1).lower())
    return ids


async def enumerate_number(client, headers, auth, airline: str,
                           number: str, since: datetime,
                           max_pages: int, out: dict) -> int:
    """Page one flight number's history; add HUEN legs to `out`."""
    page, added = 1, 0
    timestamp: datetime | str = "now"
    while page <= max_pages:
        resp = await flight_list(
            client,
            FlightListParams(flight=number, page=page, limit=100,
                             timestamp=timestamp),
            headers=headers, auth=auth,
        )
        resp.raise_for_status()
        raw = resp.json()
        response_obj = raw.get("result", {}).get("response", {})
        if not response_obj.get("data"):
            break
        df = flight_list_df(raw)
        for row in df.iter_rows(named=True):
            origin = (row.get("origin") or "").upper()
            dest = (row.get("destination") or "").upper()
            if HUEN not in (origin, dest):
                continue
            if not row.get("flight_id") or row.get("ATOD") is None:
                continue  # not operated (yet)
            hex_id = f"{int(row['flight_id']):08x}"
            if hex_id in out:
                continue
            direction = "DEP" if origin == HUEN else "ARR"
            event = row["ATOD"] if direction == "DEP" else (row.get("ATOA") or row["ATOD"])
            out[hex_id] = {
                "flight_id_hex": hex_id,
                "number": row.get("number") or number,
                "callsign": row.get("callsign") or "",
                "airline": airline,
                "direction": direction,
                "from_iata": ICAO2IATA.get(origin, origin),
                "to_iata": ICAO2IATA.get(dest, dest),
                "registration": row.get("registration") or "",
                "aircraft": row.get("aircraft") or "",
                "status": row.get("status") or "",
                "atod_utc": fmt(row.get("ATOD")),
                "atoa_utc": fmt(row.get("ATOA")),
                "stod_utc": fmt(row.get("STOD")),
                "stoa_utc": fmt(row.get("STOA")),
                "ebb_date": event.astimezone(EAT).strftime("%Y-%m-%d"),
            }
            added += 1
        oldest = df["ATOD"].min()
        if oldest is not None and oldest < since:
            break
        if not response_obj.get("page", {}).get("more"):
            break
        stod_min = df["STOD"].min()
        if stod_min is not None:
            timestamp = stod_min
        page += 1
        await asyncio.sleep(1)
    return added


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-01-01",
                    help="stop paging at this UTC date (default 2026-01-01)")
    ap.add_argument("--seed", default=str(ROOT / "flight_numbers_seed.csv"),
                    help="CSV of airline,flight_number,confidence,notes")
    ap.add_argument("--verified-only", action="store_true",
                    help="skip seed rows not marked verified")
    ap.add_argument("--max-pages", type=int, default=200,
                    help="safety cap on pages per flight number")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    seed = [r for r in csv.DictReader(open(args.seed, newline=""))
            if r.get("flight_number", "").strip()
            and (not args.verified_only
                 or r.get("confidence", "").strip() == "verified")]
    print(f"{len(seed)} flight numbers; since {args.since}")

    movements_path = ROOT / "data" / "movements.csv"
    movements = load_movements(movements_path)
    print(f"existing movements: {len(movements)}")

    headers = get_json_headers()
    async with CurlAsyncClient() as client:
        auth = await login(
            client,
            creds={"subscriptionKey": read_subscription_key(), "token": None},
        )
        print("auth:", auth.get("message"))
        for i, row in enumerate(seed, 1):
            number = row["flight_number"].strip().upper()
            try:
                added = await enumerate_number(
                    client, headers, auth, row.get("airline", "").strip(),
                    number, since, args.max_pages, movements)
            except Exception as e:
                print(f"[{i}/{len(seed)}] {number}: FAILED: {e}")
                continue
            print(f"[{i}/{len(seed)}] {number}: +{added} EBB legs "
                  f"(total {len(movements)})")

    rows = sorted(movements.values(),
                  key=lambda r: (r["ebb_date"], r["atod_utc"]))
    movements_path.parent.mkdir(parents=True, exist_ok=True)
    with open(movements_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} movements -> {movements_path}")

    have = downloaded_ids()
    targets = [r for r in rows if r["flight_id_hex"] not in have]
    targets_path = ROOT / "data" / "track_targets.csv"
    with open(targets_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["flight_id_hex", "number",
                                          "callsign", "date", "from", "to"])
        w.writeheader()
        for r in targets:
            w.writerow({"flight_id_hex": r["flight_id_hex"],
                        "number": r["number"], "callsign": r["callsign"],
                        "date": r["ebb_date"], "from": r["from_iata"],
                        "to": r["to_iata"]})
    print(f"wrote {len(targets)} track targets -> {targets_path} "
          f"({len(have)} already downloaded)")


if __name__ == "__main__":
    asyncio.run(main())
