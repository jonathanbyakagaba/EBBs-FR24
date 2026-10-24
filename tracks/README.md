# tracks/

Downloaded FR24 playback tracks land in `tracks_out/` first (git-ignored
staging). Once a batch is verified, move the CSVs here — one file per
flight, named `<NUMBER>-<flight_id_hex>.csv`:

    Timestamp,UTC,Callsign,Position,Altitude,Speed,Direction

`scripts/enumerate_ebb.py` scans this folder (and `tracks_out/`) to
work out which enumerated flights still need downloading, so anything
placed here is automatically excluded from future target lists.

Monthly zip packing (like the FR24 repo's month archives) is a later
step, once the January depth test (README, Step 0) shows how far back
usable tracks actually reach.
