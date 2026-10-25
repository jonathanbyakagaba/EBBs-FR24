"""Audit what an EBB movement board actually covers, against its seed.

Answers, without touching FR24:
  * date range, days covered, days missing inside that range
  * legs / carriers / flight numbers per day
  * per-airline totals and date span
  * seed numbers that returned nothing ("dead" numbers to fix or drop)
  * flight numbers present in the board but absent from the seed
  * carriers present in the board but absent from the seed (the blind spot
    of seed-based enumeration)

    python scripts/audit_coverage.py
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--movements", default=str(ROOT / "data" / "movements.csv"))
    ap.add_argument("--seed", default=str(ROOT / "flight_numbers_seed.csv"))
    args = ap.parse_args()

    rows = [r for r in csv.DictReader(open(args.movements, newline="")) if r.get("flight_id_hex")]
    print(f"movements: {len(rows)} legs from {args.movements}")
    if not rows:
        print("nothing to audit — the board is empty")
        return

    by_day: dict[str, list] = defaultdict(list)
    for r in rows:
        by_day[r["ebb_date"]].append(r)

    days = sorted(by_day)
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    span = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    missing = [d for d in span if str(d) not in by_day]

    print(f"range: {first} .. {last}  ({len(span)} days, {len(days)} with data)")
    if missing:
        print(f"MISSING DAYS ({len(missing)}): {', '.join(str(d) for d in missing)}")

    print("\nper day:")
    print(f"  {'date':<12}{'legs':>5}{'ARR':>5}{'DEP':>5}{'carriers':>10}{'numbers':>9}")
    for d in days:
        day_rows = by_day[d]
        arr = sum(1 for r in day_rows if r["direction"] == "ARR")
        carriers = len({r["airline"] for r in day_rows})
        numbers = len({r["number"] for r in day_rows})
        print(f"  {d:<12}{len(day_rows):>5}{arr:>5}{len(day_rows)-arr:>5}{carriers:>10}{numbers:>9}")

    print("\nper airline:")
    for airline, n in Counter(r["airline"] for r in rows).most_common():
        ds = sorted(r["ebb_date"] for r in rows if r["airline"] == airline)
        nums = sorted({r["number"] for r in rows if r["airline"] == airline})
        print(f"  {airline:<28}{n:>5} legs  {ds[0]}..{ds[-1]}  {len(nums)} number(s)")

    seed_rows = list(csv.DictReader(open(args.seed, newline="")))
    seed_numbers = [r["flight_number"].strip().upper() for r in seed_rows if r.get("flight_number", "").strip()]
    seen = {r["number"].strip().upper() for r in rows}
    dead = [n for n in seed_numbers if n not in seen]
    found = sorted(seen - set(seed_numbers))

    print(f"\nseed: {len(seed_numbers)} numbers, {len(seed_numbers) - len(dead)} returned legs")
    if dead:
        print(f"  ZERO LEGS ({len(dead)}): {', '.join(dead)}")
        print("  -> either the number is wrong/retired, or its flights predate the reachable window")
    if found:
        print(f"  IN BOARD BUT NOT IN SEED ({len(found)}): {', '.join(found)}")

    carriers = Counter(r["airline"] for r in rows)
    print("\ncoverage verdict:")
    if len(carriers) == 1:
        print("  ONLY ONE CARRIER — seed-based enumeration is a whitelist; it cannot discover")
        print("  the other airlines. Use the airport-board enumerator for real coverage.")
    else:
        print(f"  {len(carriers)} carriers present: {', '.join(sorted(carriers))}")
    print(f"  {len(seen)} distinct flight numbers across {len(rows)} legs")


if __name__ == "__main__":
    main()
