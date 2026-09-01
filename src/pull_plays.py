"""
Pull raw play-by-play data -- the source for the rushing/passing yardage
split that drive-level data alone can't provide (CFBD's Drive model has no
offense-type breakdown -- see pull_drives.py's docstring). Every offensive
play carries a play_type string (e.g. "Rush", "Pass Reception", "Sack") that
build_db.py classifies into rushing/passing/other so drive_stats_snapshots
(db/schema.sql) can compute passing yards/drive, rushing yards/drive, YPA,
and YPC alongside the drive-level rate stats pull_drives.py/build_db.py
already handle on their own.

UNLIKE every other pull_*.py script in this project, CFBD's PlaysApi.get_plays
takes year AND week (both mandatory) -- there's no single call for a whole
season the way pull_drives.py/pull_games.py have. A full season is roughly
13-15 calls (one per week actually played), so a full START_YEAR..END_YEAR
backfill is a real one-time cost (~150-200 calls) against the 1,000-calls/
month free-tier quota (same quota math pull_stats.py's docstring already
budgets around) -- affordable once, not something to redo casually.

INCREMENTAL BY DEFAULT, same idea as every other pull_*.py script here, but
at WEEK granularity rather than season granularity: main() only pulls weeks
of END_YEAR that (a) have actually been played (via GamesApi.get_calendar,
the same pattern pull_stats.py's pull_current_week_ppa_snapshot() already
uses) and (b) don't already have a raw plays_w<NN>_<season>_*.json file on
disk -- a played week's plays never change, so once pulled, they're never
re-pulled automatically. That keeps a normal run down to ~1 call (this
week's plays) instead of re-pulling the whole season every time.

File naming deliberately mirrors ppa_snapshots' own convention (week folded
into the PREFIX, before the year): plays_w<NN>_<season>_<stamp>.json. That
makes build_db.py's existing latest_snapshots() (prefix, year) grouping work
here unchanged, the same trick build_ppa_snapshots_table() already relies on.

Pass --full-history for the one-time backfill across every season
START_YEAR..END_YEAR (only ever needed once, e.g. a fresh clone). Pass
--season/--week together to force a specific single pull (e.g. re-pulling a
week CFBD corrected after the fact) even if that week's file already exists.

Usage:
    source .venv/bin/activate
    python src/pull_plays.py                          # current season, only new completed weeks (default)
    python src/pull_plays.py --full-history            # full START_YEAR..END_YEAR backfill (one-time)
    python src/pull_plays.py --season 2026 --week 3    # force a specific week
"""
import argparse
import json
import re
from datetime import datetime, timezone, date

import cfbd

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, plays_api, games_api

PLAYS_FILE_RE = re.compile(r"^plays_w(?P<week>\d+)_(?P<season>\d{4})_\d{8}T\d{6}Z\.json$")


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def already_pulled_weeks(season: int) -> set:
    """Weeks of `season` that already have a raw plays file on disk."""
    weeks = set()
    for f in RAW_DIR.glob(f"plays_w*_{season}_*.json"):
        m = PLAYS_FILE_RE.match(f.name)
        if m and int(m.group("season")) == season:
            weeks.add(int(m.group("week")))
    return weeks


def completed_weeks_for(client, season: int) -> list:
    """
    Every REGULAR-season week of `season` whose games have already been
    played, via GamesApi.get_calendar -- same pattern as pull_stats.py's
    pull_current_week_ppa_snapshot(). Postseason (bowls/CFP) is deliberately
    excluded here: MW games essentially never reach the CFP, and bowl-week
    numbering in CFBD's calendar is inconsistent enough that it's not worth
    the complexity for a handful of extra games a year -- revisit if a
    specific backtest ever needs it.
    """
    calendar = games_api(client).get_calendar(year=season)
    today = date.today()
    return sorted(
        c.week for c in calendar
        if c.season_type == cfbd.SeasonType.REGULAR and c.last_game_start.date() <= today
    )


def pull_week(api, season: int, week: int):
    plays = api.get_plays(year=season, week=week)
    # .dict(by_alias=False) for snake_case keys -- same reasoning noted in
    # every other pull_*.py script (the client's own .to_dict() gives
    # camelCase, which build_db.py does not expect).
    return [p.dict(by_alias=False) for p in plays]


def main(full_history: bool = False, season: int = None, week: int = None):
    client = get_api_client()
    api = plays_api(client)
    stamp = _stamp()

    if season is not None and week is not None:
        # Forced single-week pull -- always writes a new file even if one
        # already exists (e.g. CFBD corrected a week after the fact), same
        # override intent every other script's explicit flag carries.
        print(f"Pulling {season} week {week} plays (forced)...")
        plays = pull_week(api, season, week)
        path = RAW_DIR / f"plays_w{week:02d}_{season}_{stamp}.json"
        path.write_text(json.dumps(plays, indent=2, default=str))
        print(f"  -> {len(plays)} plays -> {path.name}")
        print("\nDone.")
        return

    seasons = range(START_YEAR, END_YEAR + 1) if full_history else [END_YEAR]
    print(f"Pulling plays for: {list(seasons)} "
          f"({'full history backfill' if full_history else 'current season only -- pass --full-history for the rest'})")

    total_calls = 0
    for yr in seasons:
        # Ask the calendar rather than hardcoding week ranges -- a past
        # season has every regular week completed by definition, but the
        # exact week count varies year to year.
        played_weeks = completed_weeks_for(client, yr)
        have = already_pulled_weeks(yr)
        to_pull = [w for w in played_weeks if w not in have]
        if not to_pull:
            print(f"{yr}: all {len(played_weeks)} played week(s) already pulled -- nothing new.")
            continue
        print(f"{yr}: pulling {len(to_pull)} new week(s) of {len(played_weeks)} played so far: {to_pull}")
        for wk in to_pull:
            plays = pull_week(api, yr, wk)
            path = RAW_DIR / f"plays_w{wk:02d}_{yr}_{stamp}.json"
            path.write_text(json.dumps(plays, indent=2, default=str))
            print(f"  {yr} week {wk}: {len(plays)} plays -> {path.name}")
            total_calls += 1

    print(f"\nDone. {total_calls} CFBD call(s) made this run. Raw snapshots written to data/raw/.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-history", action="store_true",
                         help=f"Backfill every season {START_YEAR}-{END_YEAR} (one-time; ~150-200 CFBD calls).")
    parser.add_argument("--season", type=int, default=None, help="Force-pull a single season+week (requires --week too).")
    parser.add_argument("--week", type=int, default=None, help="Force-pull a single season+week (requires --season too).")
    args = parser.parse_args()
    if (args.season is None) != (args.week is None):
        parser.error("--season and --week must be given together.")
    main(full_history=args.full_history, season=args.season, week=args.week)
