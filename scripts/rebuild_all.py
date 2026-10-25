"""Rebuild the Entebbe board from the root: every carrier, every reachable day.

Two passes, merged by FR24 flight id:

  1. BOARD PASS  - FR24's EBB airport board (arrivals + departures) for each
                   day. Discovers carriers with no seed file at all. Carries
                   the airline name and aircraft type.
  2. SEED PASS   - flight-number history for every number in
                   flight_numbers_seed.csv. Reaches further back than the
                   board, but only for numbers someone knew about.

Then writes data/track_targets.csv with EVERY enumerated flight - no
"already downloaded" filtering. Use fetch_tracks_all.py to pull them all.

    python scripts/rebuild_all.py --since 2026-10-18
    python scripts/rebuild_all.py --since 2026-01-01   # only if your FR24
                                                       # plan serves that far

429s are retried with exponential backoff, so a long run can be left alone.
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
from fr24.json import (
    AirportListParams, FlightListParams, airport_list, flight_list,
    flight_list_df, get_json_headers,
)

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


# ---------------------------------------------------------------- helpers

def fmt(ts) -> str:
    """Format either a Unix timestamp (airport board) or a datetime (flight list)."""
    if ts is None:
        return ""
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return ""


def codes(detail) -> set[str]:
    c = (detail or {}).get("code") or {}
    return {str(c[k]).upper() for k in ("icao", "iata") if c.get(k)}


def iata(detail) -> str:
    c = (detail or {}).get("code") or {}
    raw = c.get("icao") or c.get("iata") or ""
    return ICAO2IATA.get(raw, c.get("iata") or raw)


def find_flight_lists(obj, path="") -> list[tuple[str, dict]]:
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
    if isinstance(obj, dict):
        return {k: skeleton(v, depth - 1, width) for k, v in list(obj.items())[:width]} if depth > 0 else "{...}"
    if isinstance(obj, list):
        return [skeleton(obj[0], depth - 1, width), f"...{len(obj)} items"] if obj and depth > 0 else []
    return type(obj).__name__


def board_row(item: dict) -> dict | None:
    """One flight from the airport board."""
    ident = item.get("identification") or {}
    fid = ident.get("id")
    if not fid:
        return None
    times = item.get("time") or {}
    real = times.get("real") or {}
    sched = times.get("scheduled") or {}
    atod, atoa = real.get("departure"), real.get("arrival")
    origin, dest = item.get("airport", {}).get("origin"), item.get("airport", {}).get("destination")
    at_ebb_origin = bool(codes(origin) & EBB_CODES)
    at_ebb_dest = bool(codes(dest) & EBB_CODES)
    if not (at_ebb_origin or at_ebb_dest):
        return None
    if atod is None and atoa is None:
        return None
    direction = "DEP" if at_ebb_origin else "ARR"
    event = atod if direction == "DEP" else (atoa or atod)
    if event is None:
        return None
    return {
        "flight_id_hex": f"{int(fid, 16):08x}",
        "number": ((ident.get("number") or {}).get("default") or ""),
        "callsign": ident.get("callsign") or "",
        "airline": (item.get("airline") or {}).get("name") or "",
        "direction": direction,
        "from_iata": iata(origin), "to_iata": iata(dest),
        "registration": (item.get("aircraft") or {}).get("registration") or "",
        "aircraft": ((item.get("aircraft") or {}).get("model") or {}).get("code") or "",
        "status": (item.get("status") or {}).get("text") or "",
        "atod_utc": fmt(atod), "atoa_utc": fmt(atoa),
        "stod_utc": fmt(sched.get("departure")), "stoa_utc": fmt(sched.get("arrival")),
        "ebb_date": datetime.fromtimestamp(int(event), EAT).strftime("%Y-%m-%d"),
    }


def seed_row(row: dict, number: str, airline: str) -> dict | None:
    """One flight from the flight-number list (polars row)."""
    origin = (row.get("origin") or "").upper()
    dest = (row.get("destination") or "").upper()
    if origin not in EBB_CODES and dest not in EBB_CODES:
        return None
    if not row.get("flight_id") or row.get("ATOD") is None:
        return None
    direction = "DEP" if origin in EBB_CODES else "ARR"
    event = row["ATOD"] if direction == "DEP" else (row.get("ATOA") or row["ATOD"])
    return {
        "flight_id_hex": f"{int(row['flight_id']):08x}",
        "number": row.get("number") or number,
        "callsign": row.get("callsign") or "",
        "airline": airline,
        "direction": direction,
        "from_iata": iata({"code": {"icao": origin}}) if origin else "",
        "to_iata": iata({"code": {"icao": dest}}) if dest else "",
        "registration": row.get("registration") or "",
        "aircraft": row.get("typecode") or "",
        "status": row.get("status") or "",
        "atod_utc": fmt(row.get("ATOD")), "atoa_utc": fmt(row.get("ATOA")),
        "stod_utc": fmt(row.get("STOD")), "stoa_utc": fmt(row.get("STOA")),
        "ebb_date": event.astimezone(EAT).strftime("%Y-%m-%d"),
    }


def is_429(exc: Exception) -> bool:
    text = f"{exc} {getattr(exc, 'response', '')}"
    return "429" in text or "Too Many Requests" in text


async def with_retries(make_coro, label: str, attempts: int = 5, base: float = 60.0):
    """Run `make_coro()` and back off on 429 rather than giving up."""
    for attempt in range(1, attempts + 1):
        try:
            return await make_coro()
        except Exception as e:
            if is_429(e) and attempt < attempts:
                wait = base * attempt
                print(f"    {label}: 429 - waiting {wait:.0f}s (attempt {attempt}/{attempts})")
                await asyncio.sleep(wait)
                continue
            raise


# ------------------------------------------------------------------ passes

async def board_pass(client, headers, auth, days, out: dict, sleep: float, max_pages: int) -> None:
    print(f"\n== BOARD PASS ({len(days)} days, all carriers) ==")
    for day in days:
        ts = int(datetime(day.year, day.month, day.day, 23, 59, tzinfo=EAT).timestamp())
        added, blocked, unparsed = 0, 0, None
        for mode in ("arrivals", "departures"):
            page = 1
            while page <= max_pages:
                try:
                    resp = await with_retries(
                        lambda: airport_list(client, AirportListParams(
                            airport="EBB", mode=mode, page=page, limit=100, timestamp=ts),
                            headers=headers, auth=auth), f"{day} {mode}")
                except Exception as e:
                    print(f"  {day} {mode}: unavailable ({type(e).__name__}: {e})")
                    blocked += 1
                    break
                raw = resp.json()
                lists = find_flight_lists(raw)
                if not lists:
                    unparsed = raw if unparsed is None else unparsed
                    break
                node = lists[0][1]
                items = node.get("data") or []
                if not items:
                    break
                for item in items:
                    row = board_row(item)
                    if row and row["flight_id_hex"] not in out:
                        out[row["flight_id_hex"]] = row
                        added += 1
                if not (node.get("page") or {}).get("more"):
                    break
                page += 1
                await asyncio.sleep(sleep)
            await asyncio.sleep(sleep)
        if unparsed is not None and added == 0:
            print(f"  {day}: read OK but nothing parsed - payload skeleton:")
            print(json.dumps(skeleton(unparsed), indent=1)[:700])
        carriers = len({r["airline"] for r in out.values() if r["ebb_date"] == str(day)})
        note = " (board refused this day)" if blocked and not added else ""
        print(f"  {day}: +{added:<4} total {len(out):<5} {carriers:>2} carriers{note}")


async def seed_pass(client, headers, auth, seed, since: datetime, out: dict,
                    sleep: float, max_pages: int) -> None:
    print(f"\n== SEED PASS ({len(seed)} numbers, back to {since:%Y-%m-%d}) ==")
    for i, (number, airline) in enumerate(seed, 1):
        page, added, timestamp = 1, 0, "now"
        while page <= max_pages:
            try:
                resp = await with_retries(
                    lambda: flight_list(client, FlightListParams(
                        flight=number, page=page, limit=100, timestamp=timestamp),
                        headers=headers, auth=auth), number)
            except Exception as e:
                print(f"  [{i}/{len(seed)}] {number}: FAILED {type(e).__name__}: {e}")
                break
            raw = resp.json()
            if not (raw.get("result", {}).get("response", {}) or {}).get("data"):
                break
            df = flight_list_df(raw)
            for row in df.iter_rows(named=True):
                r = seed_row(row, number, airline)
                if r and r["flight_id_hex"] not in out:
                    out[r["flight_id_hex"]] = r
                    added += 1
            oldest = df["ATOD"].min()
            if oldest is not None and oldest < since:
                break
            if not (raw["result"]["response"].get("page") or {}).get("more"):
                break
            stod_min = df["STOD"].min()
            if stod_min is not None:
                timestamp = stod_min
            page += 1
            await asyncio.sleep(sleep)
        print(f"  [{i}/{len(seed)}] {number}: +{added} (total {len(out)})")
        await asyncio.sleep(sleep)


def grow_seed(seed_path: Path, discovered: list[dict]) -> int:
    """Append (airline, number) pairs the board found but the seed lacks.

    The board is the only source that *discovers* carriers; the seed pass is
    the only one that reaches further back than a couple of days. Writing the
    board's discoveries into the seed connects the two: every day the board
    runs, the seed gets closer to complete, and the next run backfills the new
    numbers over the full window.
    """
    existing = {(n, a) for n, a in load_seed(seed_path)}
    known_numbers = {n for n, _ in existing}
    new = []
    for r in discovered:
        number, airline = r["number"].strip().upper(), (r["airline"] or "").strip()
        if number and number not in known_numbers:
            new.append((airline or "unknown", number))
            known_numbers.add(number)
    if not new:
        return 0
    write_header = not seed_path.exists() or seed_path.stat().st_size == 0
    with open(seed_path, "a", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["airline", "flight_number", "confidence", "notes"])
        for airline, number in sorted(new, key=lambda x: x[1]):
            w.writerow([airline, number, "board", "discovered by rebuild_all.py"])
    return len(new)


def load_seed(path: Path) -> list[tuple[str, str]]:
    rows = list(csv.DictReader(open(path, newline="")))
    return [(r["flight_number"].strip().upper(), (r.get("airline") or "").strip())
            for r in rows if r.get("flight_number", "").strip()]


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-18", help="earliest date to try")
    ap.add_argument("--seed", default=str(ROOT / "flight_numbers_seed.csv"))
    ap.add_argument("--movements", default=str(ROOT / "data" / "movements.csv"))
    ap.add_argument("--targets", default=str(ROOT / "data" / "track_targets.csv"))
    ap.add_argument("--grow-seed", action="store_true",
                    help="append flight numbers discovered on the board to the seed file")
    ap.add_argument("--skip-board", action="store_true")
    ap.add_argument("--skip-seed", action="store_true")
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--max-pages", type=int, default=200)
    args = ap.parse_args()

    since_date = date.fromisoformat(args.since)
    since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    today = datetime.now(EAT).date()
    days = [since_date + timedelta(days=i) for i in range(max((today - since_date).days, 0) + 1)]
    seed = load_seed(Path(args.seed))
    out: dict[str, dict] = {}
    print(f"rebuilding from {since_date} .. {today} | {len(seed)} seed numbers")

    headers = get_json_headers()
    async with CurlAsyncClient() as client:
        auth = await login(client, creds={"subscriptionKey": read_subscription_key(), "token": None})
        print("auth:", auth.get("message"))
        if not args.skip_board:
            await board_pass(client, headers, auth, days, out, args.sleep, args.max_pages)
            if args.grow_seed:
                added = grow_seed(Path(args.seed), list(out.values()))
                print(f"\nseed grown: +{added} numbers discovered from the board")
                if added:
                    seed = load_seed(Path(args.seed))
                    print(f"seed now has {len(seed)} numbers")
        if not args.skip_seed:
            await seed_pass(client, headers, auth, seed, since_dt, out, args.sleep, args.max_pages)

    rows = sorted(out.values(), key=lambda r: (r["ebb_date"], r["atod_utc"]))
    write_csv(Path(args.movements), FIELDS, rows)
    print(f"\nwrote {len(rows)} movements -> {args.movements}")

    # EVERY flight gets a target row - no 'already downloaded' filtering
    targets = [{"flight_id_hex": r["flight_id_hex"], "number": r["number"],
                "callsign": r["callsign"], "date": r["ebb_date"],
                "from": r["from_iata"], "to": r["to_iata"],
                "airline": r["airline"], "aircraft": r["aircraft"]} for r in rows]
    write_csv(Path(args.targets), ["flight_id_hex", "number", "callsign", "date",
                                   "from", "to", "airline", "aircraft"], targets)
    print(f"wrote {len(targets)} track targets (ALL flights, none filtered) -> {args.targets}")

    per_day = {}
    for r in rows:
        per_day[r["ebb_date"]] = per_day.get(r["ebb_date"], 0) + 1
    print("\nlegs per day:")
    for d in sorted(per_day):
        print(f"  {d}: {per_day[d]:>3}")
    if per_day:
        print(f"  average {len(rows) / len(per_day):.1f}/day "
              f"(EBB normally sees 45-55 - lower means carriers are still missing)")

    carriers = sorted({r["airline"] for r in rows if r["airline"]})
    print(f"\n{len(carriers)} carriers: {', '.join(carriers) if carriers else '(none)'}")
    if rows:
        print(f"date range: {rows[0]['ebb_date']} .. {rows[-1]['ebb_date']}")


if __name__ == "__main__":
    asyncio.run(main())
