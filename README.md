# Entebbe-Flights

**All-airline arrivals & departures at Entebbe International (HUEN / EBB)** —
a movement board (airline, flight number, route, scheduled vs actual times,
aircraft, status) **plus a full FlightRadar24 position track for every
flight**, starting **1 January 2026**.

Companion to the [FR24](https://github.com/jonathanbyakagaba/FR24) repo,
which holds Uganda Airlines (UR) tracks in depth. This repo takes the
airport's-eye view instead: every carrier serving Entebbe — Uganda
Airlines, Ethiopian, Kenya Airways, Emirates, Qatar Airways and the rest —
for traffic, schedule and operations analyses that cut across airlines.

## How it works

Same verified machinery as the FR24 repo's gap recovery, pointed at an
airport:

1. **`scripts/enumerate_ebb.py`** — for each flight number in
   `flight_numbers_seed.csv`, page FR24's flight list backwards from now
   to `--since 2026-01-01`, keep only legs touching HUEN, and merge them
   into `data/movements.csv` (keyed by FR24 flight id, so re-runs and
   codeshares don't duplicate). It also writes `data/track_targets.csv`
   with every enumerated flight whose track isn't downloaded yet.
2. **`scripts/fetch_tracks.py`** — download each target's complete
   playback track exactly as FR24 serves it (no trimming), one CSV per
   flight: `<NUMBER>-<flight_id_hex>.csv`, columns
   `Timestamp,UTC,Callsign,Position,Altitude,Speed,Direction`.
   Resumable — files already present are skipped.

Everything runs on a Windows machine with the FR24 account logged in
(the sandbox that maintains this repo cannot reach flightradar24.com).
Auth reads the `FR24_SUBSCRIPTION_KEY` environment variable if set
(GitHub Actions / Codespaces secret), otherwise `subscription_key` from
`%LOCALAPPDATA%\fr24\fr24\fr24.conf` — **never commit or share the key
or that file, or `fr24 auth show` output**.

```powershell
py -m venv ebb-env; ebb-env\Scripts\activate
pip install "fr24[curl,polars]==0.4.0" tzdata
```

## Step 0 — the January depth test (do this first)

Flight-list history and playback-track retention are different things:
the list may reach January while FR24 no longer serves position tracks
for the oldest flights. Before queueing thousands of downloads, test:

```powershell
# UR numbers only, back to January (fast, known-good numbers)
python scripts\enumerate_ebb.py --since 2026-01-01 --verified-only
# try downloading the 5 OLDEST targets (the file is date-sorted)
python scripts\fetch_tracks.py data\track_targets.csv --limit 5
```

If January tracks come back with real points, the full plan works as
designed. If they return EMPTY, tracks effectively start later — the
board (`movements.csv`) still goes back to January, and track downloads
simply begin from the oldest date that works. Report the outcome either
way; the repo layout doesn't change.

## Then

```powershell
# Widen the seed (see below), then enumerate everything
python scripts\enumerate_ebb.py --since 2026-01-01
python scripts\fetch_tracks.py data\track_targets.csv
```

Move finished batches from `tracks_out\` into `tracks\`, commit, push.
Entebbe sees roughly 45–55 commercial movements a day, so Jan–Oct 2026
is on the order of **15,000 flights** — expect the full track download
to run in batches over days (at ~1.5 s per flight plus transfer time,
budget roughly 1–1.5 hours per 2,000 flights), and re-run
`enumerate_ebb.py` any time to top up new flights and regenerate the
remaining-targets list.

## Growing the seed

`flight_numbers_seed.csv` starts with the 36 UR flight numbers verified
in the FR24 repo (Sep–Oct 2026) plus other carriers' numbers marked
`verify`. Wrong guesses are harmless — enumeration filters everything
by HUEN — but the fastest way to complete the seed is to copy a day's
arrivals/departures off the FR24 Entebbe airport board and add every
flight number there (including freighters: Astral, Emirates SkyCargo,
Qatar Cargo…). Flight numbers change seasonally; a number that stops
flying simply stops returning legs.

## Updating from an Android tablet (or any browser)

Nothing heavy runs on the tablet itself — GitHub's cloud does the work,
the tablet is just the screen and keyboard.

**Option A — automatic: GitHub Actions (recommended for routine updates).**
One-time setup, in a browser (2 minutes): this repo's **Settings →
Secrets and variables → Actions → New repository secret**, name
`FR24_SUBSCRIPTION_KEY`, value = the `subscription_key=` line from your
`fr24.conf`. From then on, `.github/workflows/update-ebb.yml` runs
**daily at 07:15 EAT**: it enumerates the last 10 days, merges new
movements into `data/movements.csv`, downloads missing tracks into
`tracks/`, and commits. For an on-demand update from the tablet, open
the repo → **Actions** tab → *Update Entebbe movements* → **Run
workflow** (set `since`, e.g. `2026-01-01`, for a backfill window).
The secret is write-only, masked in logs, and never available to fork
pull-request workflows; if you ever suspect exposure, rotate it
(log out/in on flightradar24.com, then update the secret).

**Option B — interactive: GitHub Codespaces.** Open this repo in VS Code
on the tablet → **Code → Codespaces → Create codespace on main**. The
`.devcontainer` config prepares Python and the `fr24` library
automatically. Add `FR24_SUBSCRIPTION_KEY` once as a Codespaces secret
(GitHub **Settings → Codespaces → New secret**) — the scripts read it
from the environment. Then run the same commands as on Windows
(`python scripts/enumerate_ebb.py --since 2026-01-01`, etc.) and commit
from the codespace. This is the best seat for the first bulk backfill:
no 6-hour Actions job cap to worry about, and it's resumable.

**One untested unknown:** FR24 may treat requests from cloud datacenter
IPs (Actions/Codespaces) differently from a home connection. The Step 0
depth test doubles as the cloud test — run it first from a codespace or
a manual Actions run. If tracks come back, you're fully tablet-based;
if FR24 refuses everything (403/EMPTY across the board), keep downloads
on the Windows PC and use the tablet for viewing and editing, which
works regardless.

## Data layout

| path | what |
|---|---|
| `data/movements.csv` | the board: one row per operated EBB arrival/departure (`ebb_date` = Entebbe-local date of the arrival/departure) |
| `data/track_targets.csv` | generated: enumerated flights whose track is not yet in `tracks/` |
| `tracks/` | downloaded track CSVs, one per flight |
| `scripts/` | `enumerate_ebb.py`, `fetch_tracks.py`, `fr24_auth.py` |
| `flight_numbers_seed.csv` | flight numbers to enumerate (see above) |

## Honest limitations

- Built on the unofficial `fr24` Python library and the account's FR24
  subscription; the underlying data is FlightRadar24's. Keep use
  personal/analytical and within FR24's terms.
- Scheduled times (STOD/STOA) come from the flight list as published;
  actuals (ATOD/ATOA) are takeoff/landing, so departure "delay" includes
  taxi-out — the same caveat as the FR24 repo.
- Enumeration only finds flights whose numbers are in the seed. A
  carrier/number nobody seeds is invisible until added.
