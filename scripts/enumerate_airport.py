"""Enumerate EVERY arrival & departure at Entebbe (EBB) from FR24's airport board.

Unlike enumate_ebb.py (which can only find flights whose numbers are in a
CSV seed), this walks FR24's EBB airport board itself: every carrier, every
freighter, no seed to maintain. Two requests per day of history
(arrivals + departures), so it stays far inside rate limits.

    python scripts/enumerate_airport.py --since 2026-10-18
"""

from __future__ import annotations

import argparse
import asyncio
import csv
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
HUEN = "HUEN"
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


def icao(detail) -> str:
    return ((detail or {}).get("code") or {}).get("icao") or ""


def row_from_item(item: dict, force_dir: str) -> dict | None:
    ident = item.get("identification") or {}
    fid = ident.get("id")
    times = item.get("time") or {}
    real = times.get("real") or {}
    sched = times.get("scheduled") or {}
    atod, atoa = real.get("departure"), real.get("arrival")
    origin, dest = icao(item.get("airport", {}).get("origin")), icao(item.get("airport", {}).get("destination"))
    if not fid or HUEN not in (origin, dest):
        return None
    if atod is None and atoa is None:
        return None
    direction = "DEP" if origin == HUEN else "ARR" if dest == HUEN else force_dir
    event = atod if direction == "DEP" else (atoa or atod)
    if event is None:
        return None
    airline = (item.get("airline") or {}).get("name") or ""
    aircraft = ((item.get("aircraft") or {}).get("model") or {}).get("code") or ""
    return {
        "flight_id_hex": f"{int(fid, 16):08x}",
        "number": ((ident.get("number") or {}).get("default") or ""),
        "callsign": ident.get("callsign") or "",
        "airline": airline,
        "direction": direction,
        "from_iata": ICAO2IATA.get(origin, origin),
        "to_iata": ICAO2IATA.get(dest, dest),
        "registration": (item.get("aircraft") or {}).get("registration") or "",
        "aircraft": aircraft,
        "status": (item.get("status") or {}).get("text") or "",
        "atod_utc": fmt(atod), "atoa_utc": fmt(atoa),
        "stod_utc": fmt(sched.get("departure")), "stoa_utc": fmt(sched.get("arrival")),
        "ebb_date": datetime.fromtimestamp(int(event), EAT).strftime("%Y-%m-%d"),
    }


def load_movements(path: Path) -> dict:
    rows = {}
    if path.exists():
        for r in csv.DictReader(open(path, newline="")):
            if r.get("flight_id_hex"):
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


async def fetch_day(client, headers, auth, day: date, out: dict, max_pages: int) -> int:
    """All EBB arrivals+departures filed before the end of `day` (EAT)."""
    ts = int(datetime(day.year, day.month, day.day, 23, 59, tzinfo=EAT).timestamp())
    added = 0
    for mode in ("arrivals", "departures"):
        page = 1
        while page <= max_pages:
            resp = await airport_list(
                client,
                AirportListParams(airport="EBB", mode=mode, page=page, limit=100, timestamp=ts),
                headers=headers, auth=auth,
            )
            resp.raise_for_status()
            section = (((resp.json().get("result") or {}).get("response") or {})
                       .get("airport") or {}).get("pluginData", {}).get("schedule", {}).get(mode) or {}
            items = section.get("data") or []
            if not items:
                break
            for item in items:
                row = row_from_item(item, "ARR" if mode == "arrivals" else "DEP")
                if row and row["flight_id_hex"] not in out:
                    out[row["flight_id_hex"]] = row
                    added += 1
            if not (section.get("page") or {}).get("more"):
                break
            page += 1
            await asyncio.sleep(1.5)
        await asyncio.sleep(1.5)
    return added


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-18")
    ap.add_argument("--out", default=str(ROOT / "data" / "movements.csv"))
    ap.add_argument("--max-pages", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=1.5)
    args = ap.parse_args()

    start = date.fromisoformat(args.since)
    today = datetime.now(EAT).date()
    days = [start + timedelta(days=i) for i in range((today - start).days + 1)]

    movements_path = Path(args.out)
    movements = load_movements(movements_path)
    print(f"{len(movements)} existing movements; {len(days)} days {days[0]} .. {days[-1]}")

    headers = get_json_headers()
    async with CurlAsyncClient() as client:
        auth = await login(client, creds={"subscriptionKey": read_subscription_key(), "token": None})
        print("auth:", auth.get("message"))
        for day in days:
            try:
                added = await fetch_day(client, headers, auth, day, movements, args.max_pages)
            except Exception as e:
                print(f"{day}: FAILED {e}")
                await asyncio.sleep(args.sleep * 4)
                continue
            airlines = len({r["airline"] for r in movements.values() if r["ebb_date"] == str(day)})
            print(f"{day}: +{added} (total {len(movements)}, {airlines} carriers that day)")
            await asyncio.sleep(args.sleep)

    rows = sorted(movements.values(), key=lambda r: (r["ebb_date"], r["atod_utc"]))
    movements_path.parent.mkdir(parents=True, exist_ok=True)
    with open(movements_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} movements -> {movements_path}")

    have = downloaded_ids()
    targets = [r for r in rows if r["flight_id_hex"] not in have]
    tp = ROOT / "data" / "track_targets.csv"
    with open(tp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["flight_id_hex", "number", "callsign", "date", "from", "to"])
        w.writeheader()
        for r in targets:
            w.writerow({"flight_id_hex": r["flight_id_hex"], "number": r["number"],
                        "callsign": r["callsign"], "date": r["ebb_date"],
                        "from": r["from_iata"], "to": r["to_iata"]})
    print(f"wrote {len(targets)} track targets -> {tp} ({len(have)} downloaded)")


if __name__ == "__main__":
    asyncio.run(main())
