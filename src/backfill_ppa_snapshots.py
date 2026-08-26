"""
One-time historical backfill for ppa_snapshots (db/schema.sql) -- the table
that makes an in-season, walk-forward-safe PPA feature possible in
totals_model.py. See that file's docstring for the full reasoning; this
script is the "build the history" half of it, and src/pull_stats.py's
extra step at the bottom is the "keep it current going forward" half.

Why this has to be a separate one-time script rather than folded into the
normal pull_stats.py loop: pull_stats.py runs on every scheduled pipeline
run and would otherwise redo this ENTIRE multi-season backfill every single
time (10 seasons x ~13 weeks = 100+ extra CFBD calls per run, forever, for
data that never changes once a past week is final). Run this once, and
pull_stats.py's small addition takes over from there for the current
season only.

What it does: for every season from START_YEAR through END_YEAR, calls
GamesApi.get_calendar(year=season) to get the real regular-season week
list (rather than assuming weeks 1-15, which isn't right for every
season), then for each week W <= "the last week that's actually in the
past" calls StatsApi.get_advanced_season_stats(year=season, end_week=W) --
this is CFBD's own aggregation of that team's efficiency numbers computed
ONLY from games through week W, nothing later. Each call is saved as its
own raw snapshot file (ppa_snapshot_w<NN>_<season>_<stamp>.json), picked up
by build_db.py the same way every other raw snapshot is (see
SNAPSHOT_RE / latest_snapshots() there) -- no special-casing needed since
the week number lives in the prefix, not a new scanning mechanism.

This is a real amount of API traffic (100+ calls), so it sleeps briefly
between calls to be a polite citizen of CFBD's free tier. Expect this to
take a few minutes. It's safe to re-run (idempotent, just re-pulls and
timestamps fresh files) or to Ctrl-C and resume later -- already-written
weeks aren't re-fetched. Just delete data/raw/ppa_snapshot_*.json manually
first if you ever want to force a full clean re-pull instead.

Usage:
    source .venv/bin/activate
    python src/backfill_ppa_snapshots.py
"""
import json
import time
from datetime import datetime, timezone, date

import cfbd

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, stats_api, games_api

SLEEP_BETWEEN_CALLS = 0.5  # seconds -- polite pacing, not a documented CFBD requirement


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _already_pulled(season: int, week: int) -> bool:
    """True if any ppa_snapshot_w<week>_<season>_*.json already exists -- lets a
    Ctrl-C'd run resume without redoing already-fetched weeks."""
    prefix = f"ppa_snapshot_w{week:02d}"
    return any(RAW_DIR.glob(f"{prefix}_{season}_*.json"))


def main():
    client = get_api_client()
    stats = stats_api(client)
    games = games_api(client)
    today = date.today()
    stamp = _stamp()

    total_calls = 0
    for season in range(START_YEAR, END_YEAR + 1):
        print(f"\n{season}: fetching calendar...")
        calendar = games.get_calendar(year=season)
        # Regular season only -- see the module docstring/schema.sql comment
        # for why postseason is out of scope for this feature. Skip any week
        # whose games haven't happened yet (only relevant for the current,
        # in-progress season -- pull_stats.py's own step keeps that one
        # current going forward instead).
        weeks = sorted({
            c.week for c in calendar
            if c.season_type == cfbd.SeasonType.REGULAR and c.last_game_start.date() <= today
        })
        if not weeks:
            print(f"  no completed regular-season weeks yet for {season}, skipping.")
            continue
        print(f"  {len(weeks)} completed regular-season weeks: {weeks[0]}-{weeks[-1]}")

        for week in weeks:
            if _already_pulled(season, week):
                print(f"  week {week}: already have a snapshot, skipping.")
                continue
            adv = stats.get_advanced_season_stats(year=season, end_week=week)
            path = RAW_DIR / f"ppa_snapshot_w{week:02d}_{season}_{stamp}.json"
            path.write_text(
                json.dumps([a.dict(by_alias=False) for a in adv], indent=2, default=str)
            )
            print(f"  week {week}: {len(adv)} teams -> {path.name}")
            total_calls += 1
            time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\nDone. {total_calls} new snapshot files written to data/raw/.")
    print("Run 'python src/build_db.py' next to load these into ppa_snapshots.")


if __name__ == "__main__":
    main()
