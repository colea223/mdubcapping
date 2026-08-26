"""
Runs just the odds-refresh half of the pipeline: pull_odds_api -> build_db ->
export_site_data. Deliberately does NOT touch pull_games / pull_stats /
pull_lines / pull_venues / power_rating / features / predict_week /
update_tracker -- those are CFBD-pipeline concerns (run_pipeline.py handles
them) and re-running them here would burn CFBD quota for no reason. This is
the odds-only refresh meant to run 3x/week on its own schedule -- see
.github/workflows/odds_pull.yml.

build_db.py re-reading every raw snapshot (games, stats, lines, etc.) makes
no new API calls of its own -- it just rebuilds the DB tables from whatever's
already committed in data/raw/ -- so running it here is safe and needed to
fold the new odds_api_*.json snapshot into line_snapshots.

Usage:
    (activate your venv first, then)
    python src/run_odds_update.py
"""
import sys
import time

import pull_odds_api
import build_db
import export_site_data

STEPS = [
    ("Pull live odds (The Odds API)", pull_odds_api.main),
    ("Build DB (folds new odds snapshot into line_snapshots)", build_db.main),
    ("Update website data (docs/data/*.json)", export_site_data.main),
]


def main():
    start = time.time()
    for label, fn in STEPS:
        print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
        try:
            fn()
        except Exception as e:
            print(f"\nOdds update stopped during '{label}': {e}")
            sys.exit(1)
    print(f"\nOdds update done in {time.time() - start:.1f}s.")


if __name__ == "__main__":
    main()
