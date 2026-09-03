"""
Pull raw game results, plus a separate FCS-era pull for North Dakota State (it
won't show up in an FBS query before 2026, since it just moved up). Writes one
immutable raw JSON snapshot per year to data/raw/ -- re-running never
overwrites a prior day's pull with a different filename, so opening/closing
lines pulled at different times don't clobber each other (see pull_lines.py).

INCREMENTAL BY DEFAULT: CFBD's free tier is a hard 1,000-calls/month quota
(confirmed at https://collegefootballdata.com/api-tiers), not just a rate
limit. Seasons before END_YEAR are final and essentially never change, so
re-pulling all of [START_YEAR, END_YEAR] on every scheduled run (this script
runs via weekly_pipeline.yml, twice a week) was burning 2 calls x 11 seasons
= 22 calls per run for data that mostly hasn't moved. main() now defaults to
pulling ONLY END_YEAR (the current season) -- the only season that actually
changes week to week. Pass --full-history for the rare occasion you genuinely
need to re-pull the whole historical window (e.g. CFBD backfills/corrects an
old season, or this is a fresh clone with an empty data/raw/).

Usage:
    source .venv/bin/activate
    python src/pull_games.py                # current season only (default)
    python src/pull_games.py --full-history  # full START_YEAR..END_YEAR re-pull
"""
import argparse
import time
from datetime import datetime, timezone

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, games_api
from raw_storage import write_json_gz, prune_superseded
from teams import MW_TEAMS_2026


def _stamp():
    # NOTE: intentionally called once per script run, not inside a loop meant
    # to be deterministic/cacheable -- this is a one-shot data pull script.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def pull_fbs_games(year: int, api):
    games = api.get_games(year=year, classification="fbs")
    # NOTE: use .dict(by_alias=False), NOT the generated .to_dict() -- the
    # cfbd client's .to_dict() serializes with camelCase keys (homeTeam,
    # startDate, homePoints, ...) while build_db.py expects the snake_case
    # names (home_team, start_date, home_points, ...). Using .to_dict() here
    # silently null-fills almost every important field on the way in.
    return [g.dict(by_alias=False) for g in games]


def pull_ndsu_games(year: int, api):
    # North Dakota State was FCS every year until 2026 -- pull by team name
    # directly rather than by division, so its pre-2026 schedule comes along.
    games = api.get_games(year=year, team="North Dakota State")
    return [g.dict(by_alias=False) for g in games]


def main(full_history: bool = False):
    client = get_api_client()
    api = games_api(client)
    stamp = _stamp()

    years = range(START_YEAR, END_YEAR + 1) if full_history else [END_YEAR]
    print(f"Pulling games for: {list(years)} "
          f"({'full history' if full_history else 'current season only -- pass --full-history for the rest'})")

    for year in years:
        print(f"Pulling {year} FBS games...")
        fbs_games = pull_fbs_games(year, api)
        out_path = write_json_gz(RAW_DIR / f"games_fbs_{year}_{stamp}.json", fbs_games)
        print(f"  -> {len(fbs_games)} games -> {out_path.name}")
        removed = prune_superseded(RAW_DIR, f"games_fbs_{year}_*.json*", out_path)
        if removed:
            print(f"  pruned {len(removed)} superseded snapshot(s): {removed}")

        print(f"Pulling {year} North Dakota State games...")
        ndsu_games = pull_ndsu_games(year, api)
        out_path = write_json_gz(RAW_DIR / f"games_ndsu_{year}_{stamp}.json", ndsu_games)
        print(f"  -> {len(ndsu_games)} games -> {out_path.name}")
        removed = prune_superseded(RAW_DIR, f"games_ndsu_{year}_*.json*", out_path)
        if removed:
            print(f"  pruned {len(removed)} superseded snapshot(s): {removed}")

    print("\nDone. Raw snapshots written to data/raw/.")
    print(f"2026 MW teams tracked: {list(MW_TEAMS_2026.keys())}")


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
