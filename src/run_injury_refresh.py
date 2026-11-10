"""
Game-week refresh: injuries, lines/odds, predictions and line shopping -- WITHOUT touching the Excel tracker.

Meant to run on a schedule (see .github/workflows/injury_refresh.yml): Tue-Sat evenings and Saturday before noon,
when injury news and lines are changing. It is deliberately lighter than run_pipeline.py (no stats / drives / plays /
venues pulls, no model comparison) and deliberately does NOT run update_tracker -- the workflow that calls this
commits only data/raw/, data/clean/ and docs/data/, never excel/, so a scheduled bot commit can never collide with
edits you make to the tracker (Bet Log etc.) on your own computer.

Steps (same order the full pipeline uses, because the database is rebuilt from data/raw/ on every run):
    pull games (scores) -> pull CFBD lines (every book) -> pull Odds API -> covers.com injury report ->
    player talent/usage -> build DB -> power ratings -> game features -> injury NEWS scan -> predict upcoming week ->
    line shopping (best number at every book) -> website data

Steps marked optional never stop the run; a failure just prints a warning.

Usage:
    python src/run_injury_refresh.py
"""
import sys
import time

import pull_games
import pull_lines
import pull_odds_api
import pull_injuries
import pull_player_talent
import build_db
import power_rating
import features
import pull_injury_news
import predict_week
import best_lines
import export_site_data

# (label, function, optional)
STEPS = [
    ("Pull game results (scores/completed status)", pull_games.main, False),
    ("Pull CFBD lines (every sportsbook)", pull_lines.main, False),
    ("Pull live odds (The Odds API)", pull_odds_api.main, True),
    ("Injury report (covers.com)", pull_injuries.main, True),
    ("Player talent (rosters, recruiting, snap share)", pull_player_talent.main, True),
    ("Build DB", build_db.main, False),
    ("Recompute power ratings", power_rating.main, False),
    ("Recompute game features", features.main, False),
    ("Injury news scan (key players, every team)", pull_injury_news.main, True),
    ("Predict next upcoming week (Ridge/XGBoost/Dub Gamma)", predict_week.main, False),
    ("Line shopping (best number at every book)", best_lines.main, True),
    ("Update website data (docs/data/*.json)", export_site_data.main, False),
]


def main():
    start = time.time()
    for label, fn, optional in STEPS:
        print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            if optional:
                print(f"\n  (optional step '{label}' failed and was skipped: {type(e).__name__}: {e})")
                continue
            print(f"\nInjury refresh stopped during '{label}': {e}")
            sys.exit(1)
    print(f"\nInjury refresh done in {time.time() - start:.1f}s.")


if __name__ == "__main__":
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()
