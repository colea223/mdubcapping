"""
One-time historical backfill for player_season_ppa (db/schema.sql) --
per-player season-cumulative PPA, the "how valuable was this player"
weight behind gamma_model.py's injury term AND src/diagnose_injury_feature.py's
candidate injury_diff feature for Ridge/XGBoost.

WHY THIS IS A SEPARATE SCRIPT rather than "just run pull_stats.py
--full-history": pull_stats.py's own docstring is explicit that CFBD's free
tier is a hard 1,000-calls/month quota, and --full-history re-pulls FIVE
endpoints (advanced stats, SP+, Elo, recruiting, returning production) across
all 11 seasons = 44 calls -- almost all of which is pure waste here, since
this project already has historical coverage for those other four (that's
why sp_diff/ppa_diff/talent_diff/returning_production_diff all have real
non-null rows for old seasons already). The ONLY thing actually missing is
player_season_ppa, which pull_player_season_ppa() only started being called
for END_YEAR (2026) once it was added to pull_stats.py's normal loop -- it
was never backfilled for prior seasons before that. This script does ONLY
that one endpoint, for the years pull_stats.py's default run doesn't cover
-- 11 calls instead of 44+.

WHY PRIOR SEASONS MATTER AT ALL for a feature about THIS season's injuries:
diagnose_injury_feature.py deliberately weights a missing player by their
PRIOR season's total_ppa (not the current, in-progress season's), the same
safe-by-construction convention model.py's other *_diff features already
use -- see that script's own docstring for the full leakage reasoning. So
testing an injury feature for 2026 games specifically needs 2025's
player_season_ppa to exist, which this backfill provides.

Safe to re-run (idempotent) -- skips any year that already has a
player_ppa_<year>_*.json* raw snapshot rather than re-spending a call on it,
same resume behavior as backfill_ppa_snapshots.py. Sleeps briefly between
calls to be a polite citizen of CFBD's free tier.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python src/backfill_player_season_ppa.py
"""
import time
from datetime import datetime, timezone

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client
from pull_stats import pull_player_season_ppa

SLEEP_BETWEEN_CALLS = 0.5  # seconds -- polite pacing, not a documented CFBD requirement


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _already_pulled(year: int) -> bool:
    return any(RAW_DIR.glob(f"player_ppa_{year}_*.json*"))


def main():
    client = get_api_client()
    stamp = _stamp()

    years = list(range(START_YEAR, END_YEAR + 1))
    print(f"Backfilling player_season_ppa for {years[0]}-{years[-1]} ({len(years)} seasons)...")

    pulled, skipped = 0, 0
    for year in years:
        if _already_pulled(year):
            print(f"  {year}: already have a snapshot -- skipping (delete data/raw/player_ppa_{year}_*.json* "
                  "manually first to force a re-pull)")
            skipped += 1
            continue
        pull_player_season_ppa(client, year, stamp)
        pulled += 1
        time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"\nDone. {pulled} season(s) pulled, {skipped} already had a snapshot.")
    print("Next: run src/build_db.py to load these into player_season_ppa "
          "(this will also wipe game_features -- re-run src/features.py "
          "afterward before anything that depends on it).")


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
