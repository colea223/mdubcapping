"""
Pull advanced (PPA/EPA) season stats, SP+ ratings, Elo, and recruiting talent
composites. These are the "best measure" inputs discussed in the attack plan
-- efficiency margin as the primary signal, SP+/Elo as an external
blend/sanity-check, recruiting as the prior for thin-sample teams (this year:
UTEP, Northern Illinois, North Dakota State).

Also keeps ppa_snapshots (db/schema.sql) current for the LIVE season only --
see the pull_current_week_ppa_snapshot() docstring below and
totals_model.py's in-season PPA feature for the full reasoning. The
multi-season historical backfill behind that same table is a separate,
one-time script (src/backfill_ppa_snapshots.py) -- it would be wasteful to
redo 100+ calls of unchanging past-season history on every scheduled run.

INCREMENTAL BY DEFAULT: CFBD's free tier is a hard 1,000-calls/month quota
(confirmed at https://collegefootballdata.com/api-tiers). Past seasons'
advanced stats/SP+/Elo/recruiting are final and don't change, so re-pulling
all of [START_YEAR, END_YEAR] twice a week (via weekly_pipeline.yml) was 4
calls x 11 seasons = 44 calls per run (plus 1 more for the current-week PPA
snapshot) for data that mostly hasn't moved -- the single biggest chunk of
this project's CFBD usage. main() now defaults to pulling ONLY END_YEAR (the
current season) -- the only season whose numbers actually change week to
week. Pass --full-history for the rare case you genuinely need the whole
window again (e.g. a fresh clone, or CFBD revises an older season).

Usage:
    source .venv/bin/activate
    python src/pull_stats.py                # current season only (default)
    python src/pull_stats.py --full-history  # full START_YEAR..END_YEAR re-pull
"""
import argparse
from datetime import datetime, timezone, date

import cfbd

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, stats_api, ratings_api, recruiting_api, games_api
from raw_storage import write_json_gz, prune_superseded


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def pull_current_week_ppa_snapshot(client, stamp):
    """
    Pulls exactly one extra snapshot per run: END_YEAR's advanced stats
    through the most recent regular-season week that's actually happened,
    via GamesApi.get_calendar(year=END_YEAR) to find that week (rather than
    guessing from today's date directly -- calendar weeks don't line up
    with calendar dates in a fixed way). Saved with the same
    ppa_snapshot_w<NN>_<season>_<stamp>.json naming build_db.py already
    knows how to find (see SNAPSHOT_RE there) -- no special-casing needed.
    If it's before the season's first game (or between seasons), there's no
    completed week yet and this is a no-op.
    """
    games = games_api(client)
    calendar = games.get_calendar(year=END_YEAR)
    today = date.today()
    completed_weeks = sorted(
        c.week for c in calendar
        if c.season_type == cfbd.SeasonType.REGULAR and c.last_game_start.date() <= today
    )
    if not completed_weeks:
        print(f"No completed {END_YEAR} regular-season week yet -- skipping in-season PPA snapshot.")
        return
    week = completed_weeks[-1]
    stats = stats_api(client)
    adv = stats.get_advanced_season_stats(year=END_YEAR, end_week=week)
    prefix = f"ppa_snapshot_w{week:02d}"
    path = write_json_gz(RAW_DIR / f"{prefix}_{END_YEAR}_{stamp}.json", [a.dict(by_alias=False) for a in adv])
    print(f"In-season PPA snapshot through week {week}: {len(adv)} teams -> {path.name}")
    removed = prune_superseded(RAW_DIR, f"{prefix}_{END_YEAR}_*.json*", path)
    if removed:
        print(f"  pruned {len(removed)} superseded snapshot(s): {removed}")


def main(full_history: bool = False):
    client = get_api_client()
    stats = stats_api(client)
    ratings = ratings_api(client)
    recruiting = recruiting_api(client)
    stamp = _stamp()

    years = range(START_YEAR, END_YEAR + 1) if full_history else [END_YEAR]
    print(f"Pulling stats for: {list(years)} "
          f"({'full history' if full_history else 'current season only -- pass --full-history for the rest'})")

    # NOTE: .dict(by_alias=False), NOT .to_dict() -- see the comment in
    # pull_games.py. The generated .to_dict() serializes with camelCase keys
    # (successRate, specialTeams, ...) while build_db.py expects snake_case
    # (success_rate, special_teams, ...); using .to_dict() silently drops
    # those fields to null on the way in.
    for year in years:
        print(f"Pulling {year} advanced season stats (PPA)...")
        adv = stats.get_advanced_season_stats(year=year)
        out_path = write_json_gz(RAW_DIR / f"advanced_stats_{year}_{stamp}.json", [a.dict(by_alias=False) for a in adv])
        print(f"  -> {len(adv)} teams")
        removed = prune_superseded(RAW_DIR, f"advanced_stats_{year}_*.json*", out_path)
        if removed:
            print(f"  pruned {len(removed)} superseded snapshot(s): {removed}")

        print(f"Pulling {year} SP+ ratings...")
        sp = ratings.get_sp(year=year)
        out_path = write_json_gz(RAW_DIR / f"sp_ratings_{year}_{stamp}.json", [s.dict(by_alias=False) for s in sp])
        print(f"  -> {len(sp)} teams")
        removed = prune_superseded(RAW_DIR, f"sp_ratings_{year}_*.json*", out_path)
        if removed:
            print(f"  pruned {len(removed)} superseded snapshot(s): {removed}")

        print(f"Pulling {year} Elo ratings...")
        elo = ratings.get_elo(year=year)
        out_path = write_json_gz(RAW_DIR / f"elo_ratings_{year}_{stamp}.json", [e.dict(by_alias=False) for e in elo])
        print(f"  -> {len(elo)} teams")
        removed = prune_superseded(RAW_DIR, f"elo_ratings_{year}_*.json*", out_path)
        if removed:
            print(f"  pruned {len(removed)} superseded snapshot(s): {removed}")

        print(f"Pulling {year} recruiting composite...")
        rec = recruiting.get_team_recruiting_rankings(year=year)
        out_path = write_json_gz(RAW_DIR / f"recruiting_{year}_{stamp}.json", [r.dict(by_alias=False) for r in rec])
        print(f"  -> {len(rec)} teams")
        removed = prune_superseded(RAW_DIR, f"recruiting_{year}_*.json*", out_path)
        if removed:
            print(f"  pruned {len(removed)} superseded snapshot(s): {removed}")

    pull_current_week_ppa_snapshot(client, stamp)

    print("\nDone. Raw snapshots written to data/raw/.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-history", action="store_true",
                         help=f"Re-pull every season {START_YEAR}-{END_YEAR} instead of just {END_YEAR}.")
    args = parser.parse_args()
    main(full_history=args.full_history)
