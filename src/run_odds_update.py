"""
Runs the odds + game-results refresh half of the pipeline: pull_games ->
pull_odds_api -> build_db -> export_site_data. Deliberately does NOT touch
pull_stats / pull_lines / pull_venues / power_rating / features /
predict_week / update_tracker -- those are CFBD full-pipeline concerns
(run_pipeline.py handles them) and re-running them here would burn CFBD
quota and compute for no reason. This is the lighter refresh meant to run
Mon-Fri on its own schedule -- see .github/workflows/odds_pull.yml.

pull_games.main() was added alongside pull_odds_api so that a game's
completed/home_points/away_points show up on the site (Matchups/Predictions
Final scores, prediction correctness) within a day of it finishing, rather
than waiting for weekly_pipeline.yml's Sun/Wed schedule. It's a cheap add --
pull_games.py already defaults to pulling only the current season (2 CFBD
calls: FBS + NDSU), so this costs ~2 calls/weekday on top of what this
script already used, still trivial against CFBD's 1,000/month cap. It does
NOT re-run pull_stats/features/predict_week, so newly-completed games get
their score/completed flag refreshed here, but a brand new game's model
features/prediction still only appear after the next full pipeline run --
this only affects games already known about.

build_db.py re-reading every raw snapshot (games, stats, lines, etc.) makes
no new API calls of its own -- it just rebuilds the DB tables from whatever's
already committed in data/raw/ -- so running it here is safe and needed to
fold both the new games snapshot and the new odds_api_*.json snapshot into
their respective tables.

Usage:
    (activate your venv first, then)
    python src/run_odds_update.py
"""
import sys
import time

import pull_games
import pull_odds_api
import build_db
import export_site_data

STEPS = [
    ("Pull game results (scores/completed status)", pull_games.main),
    ("Pull live odds (The Odds API)", pull_odds_api.main),
    ("Build DB (folds new games + odds snapshots in)", build_db.main),
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
