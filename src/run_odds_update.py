"""
Runs the odds + game-results refresh half of the pipeline: pull_games ->
pull_odds_api -> build_db -> power_rating -> features -> predict_week ->
update_tracker -> export_site_data. Deliberately does NOT touch pull_stats /
pull_lines / pull_venues -- those are CFBD full-pipeline concerns
(run_pipeline.py handles them) and re-running them here would burn CFBD
quota for no reason. This is the lighter refresh meant to run Tue-Sat (plus
Sunday morning) on its own schedule -- see .github/workflows/odds_pull.yml.

predict_week.py/update_tracker.py WERE excluded here (added per a later
request from Cole) -- without them, this daily-ish refresh left the live
model's own predictions (and Dub Beta/Dub Gamma alongside it) frozen between
full pipeline runs, so a game could show a fresh Final score here while its
model prediction was still several days stale. predict_week.py only
refits/re-searches for the ONE upcoming week (not a full historical
walk-forward the way model_comparison.py is), so it's cheap enough to run
here too -- see that script's own docstring for its own cost profile.

pull_games.main() was added alongside pull_odds_api so that a game's
completed/home_points/away_points show up on the site (Matchups/Predictions
Final scores, prediction correctness) within a day of it finishing, rather
than waiting for weekly_pipeline.yml's Sun/Wed schedule. It's a cheap add --
pull_games.py already defaults to pulling only the current season (2 CFBD
calls: FBS + NDSU), so this costs ~2 calls/weekday on top of what this
script already used, still trivial against CFBD's 1,000/month cap. It does
NOT re-run pull_stats/predict_week, so newly-completed games get their
score/completed flag refreshed here, but a brand new game's SEASON-level
stats (advanced_stats/sp_ratings/recruiting/returning_production/etc, all
CFBD pulls) still only appear after the next full pipeline run -- this only
affects games already known about.

build_db.py re-reading every raw snapshot (games, stats, lines, etc.) makes
no new API calls of its own -- it just rebuilds the DB tables from whatever's
already committed in data/raw/ -- so running it here is safe and needed to
fold both the new games snapshot and the new odds_api_*.json snapshot into
their respective tables.

power_rating.py and features.py are BOTH included here (unlike predict_week/
update_tracker) even though they weren't originally -- and this is the fix
for a real bug, not scope creep: every GitHub Actions run starts from a
fresh checkout with NO persisted .duckdb file (see db/*.duckdb in
.gitignore), so every table -- game_features very much included -- is
rebuilt from data/raw/ from scratch on every run, in every workflow. Before
this fix, this script only ran build_db.py, which never touches
ratings_baseline (power_rating.py) or game_features (features.py) at all --
so every single run of this workflow hit export_site_data.py with an
EMPTY game_features table, and build_matchup_grid() there calls
model.fit_margin_model() on that empty frame unconditionally (no row-count
guard), which crashes immediately with sklearn's "Found array with 0
sample(s)" -- failing the whole job before the "Commit updated data" step
ever ran, so not even that day's score/odds pull was getting saved. Neither
power_rating.py nor features.py makes any API calls of their own (both are
pure local recomputation from tables build_db.py already loaded -- see
their own imports), so adding them here costs zero extra CFBD/Odds API
quota; it just means Mon-Fri runs now get a genuinely fresh game_features/
ratings_baseline/sos_ratings, so predict-page data on the site is as current
as that day's games/lines rather than frozen until the next Sun/Wed full run.

Usage:
    (activate your venv first, then)
    python src/run_odds_update.py
"""
import sys
import time

import sys as _sys
from pathlib import Path as _Path

import pull_games
import pull_odds_api
import build_db
import power_rating
import features
import predict_week
import export_site_data

_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "excel"))
import update_tracker  # noqa: E402

STEPS = [
    ("Pull game results (scores/completed status)", pull_games.main),
    ("Pull live odds (The Odds API)", pull_odds_api.main),
    ("Build DB (folds new games + odds snapshots in)", build_db.main),
    ("Recompute power ratings (Elo + strength of schedule)", power_rating.main),
    ("Recompute game features (game_features table)", features.main),
    # Added per Cole's request -- previously this daily refresh left the
    # live model / Dub Beta / Dub Gamma predictions frozen until the next
    # Sun/Mon/Wed full pipeline run (predict_week.py wasn't in this script's
    # step list at all), so the site could show a Final score from today's
    # games sitting next to a model prediction that was still several days
    # stale. predict_week.py only refits/re-searches for ONE upcoming week
    # (not a full historical walk-forward like model_comparison.py), so it's
    # cheap enough to run daily -- see that script's own docstring.
    ("Predict next upcoming week (Ridge/XGBoost/Dub Gamma)", predict_week.main),
    ("Update Excel tracker's Weekly Slate", update_tracker.main),
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
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
