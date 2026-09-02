"""
Runs the full pipeline in order: pull_games -> pull_stats -> pull_lines ->
pull_venues -> pull_drives -> pull_plays -> build_db.
Cross-platform (Windows/Mac/Linux) since it's plain Python, not a shell script.

NOTE on CFBD call volume: pull_games/pull_stats/pull_lines/pull_drives now
default to pulling ONLY the current season (see each script's own docstring)
rather than re-pulling the full 2016+ history every run -- CFBD's free tier
is a hard 1,000-calls/month quota, and re-pulling 11 unchanging historical
seasons twice a week was eating most of it. This script always calls each
module's main() with no arguments, so it always gets that incremental
(current-season-only) behavior -- pull_plays.main() with no arguments is
incremental at WEEK granularity instead (only newly-completed weeks of the
current season, ~1 call each), same idea. If you ever genuinely need a full
historical re-pull (fresh clone, CFBD revises an old season, or a brand-new
data/raw/ with no plays history at all yet), run the relevant pull_*.py
script directly with --full-history instead of through this script, then run
this script normally afterward.

Drives and plays are pulled here (added alongside the drive-based rate-stat
features) specifically so the CURRENT season's drive/play data -- and
therefore drive_stats_snapshots/the pass-rush split -- actually stays current
week to week, the same as every other input. Both are cheap per run: drives
is one call for the whole season (like pull_games), and plays only costs a
call for whatever week(s) just finished since the last run.

Usage:
    (activate your venv first, then)
    python src/run_pipeline.py
"""
import sys
import time
from pathlib import Path

import pull_games
import pull_stats
import pull_lines
import pull_venues
import pull_drives
import pull_plays
import build_db
import power_rating
import features
import predict_week
import export_site_data

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "excel"))
import update_tracker  # noqa: E402

STEPS = [
    ("Games", pull_games.main),
    ("Stats (PPA/SP+/Elo/Recruiting)", pull_stats.main),
    ("Lines", pull_lines.main),
    ("Venues", pull_venues.main),
    ("Drives", pull_drives.main),
    ("Plays", pull_plays.main),
    ("Build DB", build_db.main),
    ("Baseline power ratings", power_rating.main),
    ("Pre-game features", features.main),
    ("Predict next upcoming week", predict_week.main),
    ("Update Excel tracker", update_tracker.main),
    ("Update website data (docs/data/*.json)", export_site_data.main),
]

# Not included above -- run these on their own when you want them, not on
# every refresh:
#   python src/backtest.py                    (an evaluation report, not a data step)
#   python src/predict_week.py --season .. --week ..   (for a week other than the next upcoming one)
#   python src/model_comparison.py             (Ridge vs. XGBoost evaluation -- slower than
#                                                backtest.py alone since it refits an XGBoost
#                                                hyperparameter search per test week; see its
#                                                own docstring. Follow with
#                                                excel/update_model_comparison_tab.py to push
#                                                the result into the tracker's "Model
#                                                Comparison" tab. Informational only -- Ridge
#                                                stays the live model in every step above.)


def main():
    start = time.time()
    for label, fn in STEPS:
        print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
        try:
            fn()
        except Exception as e:
            print(f"\nPipeline stopped during '{label}': {e}")
            sys.exit(1)
    print(f"\nAll steps done in {time.time() - start:.1f}s.")


if __name__ == "__main__":
    main()
