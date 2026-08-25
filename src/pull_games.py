"""
Pull raw game results for every FBS season in [START_YEAR, END_YEAR], plus a
separate FCS-era pull for North Dakota State (it won't show up in an FBS query
before 2026, since it just moved up). Writes one immutable raw JSON snapshot
per year to data/raw/ -- re-running never overwrites a prior day's pull with a
different filename, so opening/closing lines pulled at different times don't
clobber each other (see pull_lines.py).

Usage:
    source .venv/bin/activate
    python src/pull_games.py
"""
import json
from datetime import datetime, timezone

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, games_api
from teams import MW_TEAMS_2026


def _stamp():
    # NOTE: intentionally called once per script run, not inside a loop meant
    # to be deterministic/cacheable -- this is a one-shot data pull script.
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def pull_fbs_games(year: int, api):
    games = api.get_games(year=year, classification="fbs")
    return [g.to_dict() for g in games]


def pull_ndsu_games(year: int, api):
    # North Dakota State was FCS every year until 2026 -- pull by team name
    # directly rather than by division, so its pre-2026 schedule comes along.
    games = api.get_games(year=year, team="North Dakota State")
    return [g.to_dict() for g in games]


def main():
    client = get_api_client()
    api = games_api(client)
    stamp = _stamp()

    for year in range(START_YEAR, END_YEAR + 1):
        print(f"Pulling {year} FBS games...")
        fbs_games = pull_fbs_games(year, api)
        out_path = RAW_DIR / f"games_fbs_{year}_{stamp}.json"
        out_path.write_text(json.dumps(fbs_games, indent=2, default=str))
        print(f"  -> {len(fbs_games)} games -> {out_path.name}")

        print(f"Pulling {year} North Dakota State games...")
        ndsu_games = pull_ndsu_games(year, api)
        out_path = RAW_DIR / f"games_ndsu_{year}_{stamp}.json"
        out_path.write_text(json.dumps(ndsu_games, indent=2, default=str))
        print(f"  -> {len(ndsu_games)} games -> {out_path.name}")

    print("\nDone. Raw snapshots written to data/raw/.")
    print(f"2026 MW teams tracked: {list(MW_TEAMS_2026.keys())}")


if __name__ == "__main__":
    main()
