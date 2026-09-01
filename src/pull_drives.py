"""
Pull raw drive-level data (one row per offensive possession) -- the
foundation for drive-based rate stats (yards/drive, points/drive,
turnovers/drive, drives/game) that a real backtest (see
diagnose_drive_stats_bias.py once it exists, or just backtest.py directly)
can confirm actually help before they're trusted. CFBD's DrivesApi.get_drives
takes `year` alone (no week required) and returns every FBS drive for the
WHOLE season in one call -- same one-call-per-year shape as pull_games.py,
not the much more expensive per-week shape pull_plays.py has to use.

INCREMENTAL BY DEFAULT: same reasoning as pull_games.py/pull_lines.py/
pull_stats.py -- a past season's drives are locked in forever, so main()
defaults to pulling ONLY END_YEAR. Pass --full-history for a fresh clone or
a genuine backfill.

Usage:
    source .venv/bin/activate
    python src/pull_drives.py                # current season only (default)
    python src/pull_drives.py --full-history  # full START_YEAR..END_YEAR re-pull
"""
import argparse
import json
from datetime import datetime, timezone

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, drives_api


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def pull_drives(year: int, api):
    drives = api.get_drives(year=year)
    # .dict(by_alias=False) for snake_case keys -- same reasoning as
    # pull_games.py's identical note: the client's own .to_dict() gives
    # camelCase (driveId, gameId, ...) which build_db.py does not expect.
    return [d.dict(by_alias=False) for d in drives]


def main(full_history: bool = False):
    client = get_api_client()
    api = drives_api(client)
    stamp = _stamp()

    years = range(START_YEAR, END_YEAR + 1) if full_history else [END_YEAR]
    print(f"Pulling drives for: {list(years)} "
          f"({'full history' if full_history else 'current season only -- pass --full-history for the rest'})")

    for year in years:
        print(f"Pulling {year} drives...")
        drives = pull_drives(year, api)
        out_path = RAW_DIR / f"drives_{year}_{stamp}.json"
        out_path.write_text(json.dumps(drives, indent=2, default=str))
        print(f"  -> {len(drives)} drives -> {out_path.name}")

    print("\nDone. Raw snapshots written to data/raw/.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-history", action="store_true",
                         help=f"Re-pull every season {START_YEAR}-{END_YEAR} instead of just {END_YEAR}.")
    args = parser.parse_args()
    main(full_history=args.full_history)
