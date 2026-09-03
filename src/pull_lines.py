"""
Pull betting lines from CFBD (spotty before ~2015, decent recent seasons).
Run this TWICE per week during the season and keep both snapshots -- once
right after lines open, once right before kickoff -- since the gap between
opening and closing lines is exactly what CLV (closing line value) backtesting
measures. The timestamp in the filename keeps every pull distinct; nothing here
overwrites a prior snapshot.

CFBD's own line coverage has real gaps historically -- for full historical
closing lines (older seasons), supplement with a manually downloaded archive
(e.g. Sports Book Review Online season files) dropped into data/raw/ as
lines_external_<year>.csv; pull_lines.py only handles the CFBD side.

INCREMENTAL BY DEFAULT: CFBD's free tier is a hard 1,000-calls/month quota
(confirmed at https://collegefootballdata.com/api-tiers). Past seasons' lines
are final and don't change, so re-pulling all of [START_YEAR, END_YEAR] twice
a week (via weekly_pipeline.yml) was 11 calls per run for data that never
moves. main() now defaults to pulling ONLY END_YEAR (the current season) --
exactly the season whose lines are actually opening/closing right now. Pass
--full-history for the rare case you genuinely need the whole window again
(e.g. a fresh clone, or CFBD backfills older seasons).

Usage:
    source .venv/bin/activate
    python src/pull_lines.py                # current season only (default)
    python src/pull_lines.py --full-history  # full START_YEAR..END_YEAR re-pull
"""
import argparse
import time
from datetime import datetime, timezone

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, betting_api
from raw_storage import write_json_gz


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main(full_history: bool = False):
    client = get_api_client()
    api = betting_api(client)
    stamp = _stamp()

    years = range(START_YEAR, END_YEAR + 1) if full_history else [END_YEAR]
    print(f"Pulling lines for: {list(years)} "
          f"({'full history' if full_history else 'current season only -- pass --full-history for the rest'})")

    for year in years:
        print(f"Pulling {year} betting lines...")
        lines = api.get_lines(year=year)
        # .dict(by_alias=False), not .to_dict() -- see pull_games.py's comment.
        # This model's home_team/away_team/spread_open/over_under/moneyline
        # fields all alias to camelCase, which build_db.py's snake_case
        # .get() lookups would otherwise silently miss.
        # Compression only -- NEVER pruned. build_db.py's all_lines_snapshots()
        # deliberately scans EVERY historical snapshot for the current season
        # to power the website's Line History chart, so every timestamped
        # pull here has to survive; see the module docstring above.
        out_path = write_json_gz(RAW_DIR / f"lines_{year}_{stamp}.json", [l.dict(by_alias=False) for l in lines])
        print(f"  -> {len(lines)} games with line data -> {out_path.name}")

    print("\nDone. Raw snapshots written to data/raw/.")
    print("Reminder: re-run this weekly (opening + closing) once the season starts.")


if __name__ == "__main__":
    _script_start_time = time.time()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-history", action="store_true",
                         help=f"Re-pull every season {START_YEAR}-{END_YEAR} instead of just {END_YEAR}.")
    args = parser.parse_args()
    main(full_history=args.full_history)

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
