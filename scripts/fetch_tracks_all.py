"""Download the COMPLETE track for every flight on the board - no skipping.

Unlike fetch_tracks.py, this does not consult what is already on disk: every
row in the targets CSV is downloaded and written, so the result is the full
set of files for the full set of flights. (--skip-existing restores the old
resumable behaviour if you ever want it.)

Each file is FR24's complete playback track, untouched:

    Timestamp,UTC,Callsign,Position,Altitude,Speed,Direction

named <NUMBER>-<flight_id_hex>.csv. 429s back off exponentially, so a long
run can be left alone. A manifest of every attempt is written alongside.

    python scripts/fetch_tracks_all.py data/track_targets.csv --out tracks
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from fr24.authentication import login
from fr24.clients.curl import CurlAsyncClient
from fr24.json import PlaybackParams, get_json_headers, playback, playback_df

sys.path.insert(0, str(Path(__file__).parent))
from fr24_auth import read_subscription_key  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def is_429(exc: Exception) -> bool:
    return "429" in f"{exc} {getattr(exc, 'response', '')}"


async def fetch_one(client, headers, auth, target: dict, out_dir: Path,
                    skip_existing: bool) -> dict:
    hex_id = (target.get("flight_id_hex") or "").strip().lower().removeprefix("0x")
    number = (target.get("number") or "UNKNOWN").strip()
    dest = out_dir / f"{number}-{hex_id}.csv"
    rec = {"flight_id_hex": hex_id, "number": number, "date": target.get("date", ""),
           "status": "", "points": "", "first_utc": "", "last_utc": "",
           "file": dest.name, "error": ""}
    if skip_existing and dest.exists() and dest.stat().st_size > 0:
        rec["status"] = "skipped"
        return rec

    resp = await playback(client, PlaybackParams(flight_id=hex_id), headers=headers, auth=auth)
    resp.raise_for_status()
    raw = resp.json()
    df = playback_df(raw)
    if df.height == 0:
        rec["status"] = "empty"
        return rec

    callsign = target.get("callsign") or ""
    try:
        flight = raw["result"]["response"]["data"]["flight"]
        callsign = flight["identification"]["callsign"] or callsign
    except Exception:
        pass

    with dest.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Timestamp", "UTC", "Callsign", "Position",
                    "Altitude", "Speed", "Direction"])
        for row in df.iter_rows(named=True):
            ts = int(row["timestamp"])
            w.writerow([ts,
                        datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        callsign,
                        f"{row['latitude']:.6f},{row['longitude']:.6f}",
                        int(row["altitude"]), int(row["ground_speed"]), int(row["track"])])

    first = datetime.fromtimestamp(int(df["timestamp"].min()), timezone.utc)
    last = datetime.fromtimestamp(int(df["timestamp"].max()), timezone.utc)
    rec.update(status="ok", points=df.height,
               first_utc=first.strftime("%Y-%m-%dT%H:%M:%SZ"),
               last_utc=last.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return rec


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", help="targets CSV from rebuild_all.py")
    ap.add_argument("--out", default="tracks", help="output directory")
    ap.add_argument("--manifest", default=str(ROOT / "data" / "track_manifest.csv"))
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--skip-existing", action="store_true",
                    help="re-enable resumable mode (default: download every row)")
    args = ap.parse_args()

    targets = list(csv.DictReader(open(args.targets, newline="")))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"{len(targets)} targets -> {out_dir}/  "
          f"({'skip existing' if args.skip_existing else 'download ALL'})")

    headers = get_json_headers()
    records: list[dict] = []
    async with CurlAsyncClient() as client:
        auth = await login(client, creds={"subscriptionKey": read_subscription_key(), "token": None})
        print("auth:", auth.get("message"))
        for i, target in enumerate(targets, 1):
            for attempt in range(1, args.retries + 1):
                try:
                    rec = await fetch_one(client, headers, auth, target, out_dir,
                                          args.skip_existing)
                    break
                except Exception as e:
                    if is_429(e) and attempt < args.retries:
                        wait = 60 * attempt
                        print(f"[{i}/{len(targets)}] 429 - waiting {wait}s "
                              f"(attempt {attempt}/{args.retries})")
                        await asyncio.sleep(wait)
                        continue
                    rec = {"flight_id_hex": target.get("flight_id_hex", ""),
                           "number": target.get("number", ""), "date": target.get("date", ""),
                           "status": "failed", "points": "", "first_utc": "", "last_utc": "",
                           "file": "", "error": f"{type(e).__name__}: {e}"}
                    break
            records.append(rec)
            detail = (f"{rec['points']} points, {rec['first_utc']} .. {rec['last_utc']}"
                      if rec["status"] == "ok" else rec["error"] or "")
            print(f"[{i}/{len(targets)}] {rec['number']:<8} {rec['status']:<8} "
                  f"{rec['date']}  {detail}")
            if i < len(targets) and rec["status"] != "skipped":
                await asyncio.sleep(args.delay)

    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    with open(args.manifest, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["flight_id_hex", "number", "date", "status",
                                          "points", "first_utc", "last_utc", "file", "error"])
        w.writeheader()
        w.writerows(records)

    counts: dict[str, int] = {}
    for r in records:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    points = sum(int(r["points"]) for r in records if r["status"] == "ok")
    print(f"\nfinished: {counts} | {points} track points | manifest -> {args.manifest}")
    if counts.get("failed") or counts.get("empty"):
        print("re-run the same command to retry only what is missing (with --skip-existing), "
              "or without it to re-fetch everything.")


if __name__ == "__main__":
    asyncio.run(main())
