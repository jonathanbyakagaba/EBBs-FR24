"""Write a daily report of what a run added to the repo.

Used twice per workflow run, around the download:

    python scripts/report_daily.py --snapshot    # before: remember the state
    ...                                          # rebuild + fetch tracks
    python scripts/report_daily.py --report      # after: diff and write report

The snapshot lives in /tmp (same job, not committed), so nothing churns in
git. The report itself is committed to data/reports/ and a one-line summary
is appended to data/reports/history.csv. The full markdown is also written
to /tmp/ebb_report.md for the email step.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EAT = timezone(timedelta(hours=3))
SNAPSHOT = Path("/tmp/ebb_snapshot.json")
REPORT_MD = Path("/tmp/ebb_report.md")
SUBJECT = Path("/tmp/ebb_subject.txt")


def load_board(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {r["flight_id_hex"]: r for r in csv.DictReader(open(path, newline=""))
            if r.get("flight_id_hex")}


def load_tracks(folder: Path) -> dict[str, int]:
    """filename -> line count (0 if unreadable)."""
    out: dict[str, int] = {}
    if folder.exists():
        for fp in sorted(folder.glob("*.csv")):
            try:
                with fp.open("rb") as f:
                    out[fp.name] = max(sum(1 for _ in f) - 1, 0)
            except OSError:
                out[fp.name] = 0
    return out


def take_snapshot(board: Path, tracks: Path) -> None:
    state = {"taken_utc": datetime.now(timezone.utc).isoformat(),
             "movements": load_board(board), "tracks": load_tracks(tracks)}
    SNAPSHOT.write_text(json.dumps(state))
    print(f"snapshot: {len(state['movements'])} movements, {len(state['tracks'])} track files")


def report(board_path: Path, tracks_path: Path, reports_dir: Path) -> int:
    now = datetime.now(EAT)
    movements = load_board(board_path)
    tracks = load_tracks(tracks_path)

    prev = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else None
    first_run = prev is None
    prev_mv: dict[str, dict] = prev["movements"] if prev else {}
    prev_tr: dict[str, int] = prev["tracks"] if prev else {}

    added_ids = [i for i in movements if i not in prev_mv]
    removed_ids = [i for i in prev_mv if i not in movements]
    added = [movements[i] for i in added_ids]
    new_tracks = [f for f in tracks if f not in prev_tr]
    new_points = sum(tracks[f] for f in new_tracks)

    def by(key):
        return Counter(r[key] or "(blank)" for r in added)

    # coverage of the whole board, not just the delta
    per_day = Counter(r["ebb_date"] for r in movements.values())
    days = sorted(per_day)
    carriers = sorted({r["airline"] for r in movements.values() if r["airline"]})
    span = f"{days[0]} .. {days[-1]}" if days else "empty"
    full_span = []
    if days:
        d0, d1 = datetime.fromisoformat(days[0]).date(), datetime.fromisoformat(days[-1]).date()
        full_span = [str(d0 + timedelta(days=n)) for n in range((d1 - d0).days + 1)]
    gaps = [d for d in full_span if d not in per_day]
    recent = [(d, per_day[d]) for d in days[-7:]]
    avg = statistics.mean(per_day.values()) if per_day else 0

    lines: list[str] = []
    a = lines.append
    a(f"# Entebbe movements — report {now:%Y-%m-%d %H:%M} EAT")
    a("")
    a(f"Run finished {now:%Y-%m-%d %H:%M} EAT ({now.astimezone(timezone.utc):%H:%M}Z).")
    if first_run:
        a("")
        a("_No snapshot was found, so this report describes the full current state "
          "rather than the change since the previous run._")
    a("")
    a("## Added in this run")
    a("")
    a(f"- **{len(added)} movements** ({by('direction').get('ARR', 0)} ARR / "
      f"{by('direction').get('DEP', 0)} DEP), {len({r['number'] for r in added})} distinct flight numbers")
    a(f"- **{len(new_tracks)} track files**, {new_points:,} position points")
    if removed_ids:
        a(f"- {len(removed_ids)} movements no longer present (superseded rows)")
    a("")
    if added:
        a("### By airline")
        a("")
        a("| airline | movements |")
        a("|---|---|")
        for al, n in by("airline").most_common():
            a(f"| {al} | {n} |")
        a("")
        a("### New flight numbers")
        a("")
        nums = sorted({r["number"] for r in added if r["number"]})
        a(", ".join(f"`{n}`" for n in nums) if nums else "_none_")
        a("")
        dates = Counter(r["ebb_date"] for r in added)
        a("### By Entebbe date")
        a("")
        a("| date | movements added |")
        a("|---|---|")
        for d in sorted(dates):
            a(f"| {d} | {dates[d]} |")
        a("")

    a("## Board totals")
    a("")
    a(f"- {len(movements):,} movements across {len(days)} days ({span})")
    a(f"- {len(carriers)} carriers: {', '.join(carriers) if carriers else '(none)'}")
    a(f"- {len(tracks):,} track files on disk")
    if gaps:
        a(f"- **{len(gaps)} missing day(s)** in range: {', '.join(gaps[:10])}"
          f"{' …' if len(gaps) > 10 else ''}")
    a("")
    a("### Last 7 days")
    a("")
    a("| date | movements |")
    a("|---|---|")
    for d, n in recent:
        a(f"| {d} | {n} |")
    a("")
    a(f"Average {avg:.1f} movements/day (EBB normally sees 45–55; lower means "
      "carriers are still missing from the seed).")

    manifest = ROOT / "data" / "track_manifest.csv"
    if manifest.exists():
        counts = Counter(r["status"] for r in csv.DictReader(open(manifest, newline="")))
        if counts:
            a("")
            a("## Track download result (last run)")
            a("")
            a(f"- {counts.get('ok', 0)} downloaded, {counts.get('empty', 0)} empty, "
              f"{counts.get('failed', 0)} failed, {counts.get('skipped', 0)} skipped")

    text = "\n".join(lines) + "\n"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / f"{now:%Y-%m-%d-%H%M}.md"
    out.write_text(text)
    REPORT_MD.write_text(text)
    SUBJECT.write_text(
        f"EBB daily report {now:%Y-%m-%d %H:%M} EAT: +{len(added)} movements, "
        f"+{len(new_tracks)} tracks, {len(movements)} total\n")

    hist = reports_dir / "history.csv"
    new_file = not hist.exists()
    with hist.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["run_utc", "movements_added", "tracks_added", "track_points_added",
                        "movements_total", "tracks_total", "carriers", "avg_per_day"])
        w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    len(added), len(new_tracks), new_points, len(movements),
                    len(tracks), len(carriers), f"{avg:.1f}"])

    print(text)
    print(f"wrote {out}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", action="store_true", help="record state before the run")
    ap.add_argument("--report", action="store_true", help="diff against the snapshot")
    ap.add_argument("--movements", default=str(ROOT / "data" / "movements.csv"))
    ap.add_argument("--tracks", default=str(ROOT / "tracks"))
    ap.add_argument("--reports-dir", default=str(ROOT / "data" / "reports"))
    args = ap.parse_args()

    if args.snapshot:
        take_snapshot(Path(args.movements), Path(args.tracks))
    elif args.report:
        report(Path(args.movements), Path(args.tracks), Path(args.reports_dir))
    else:
        ap.error("choose --snapshot or --report")


if __name__ == "__main__":
    main()
