"""
Investigates a real question Cole raised: Ridge and XGBoost both train on
the FULL 10-season history in the DB every time they're fit, but almost
nobody on a 2016 (or even a 2019) roster is still playing college football
in 2026 -- so does blending a full decade of games into one fixed fit
introduce bias on TODAY's games, relative to training on just the last
handful of seasons?

WHY THIS ISN'T QUITE THE BIAS IT SOUNDS LIKE AT FIRST: every feature in
game_features is already scoped to describe a team AS OF that game's own
season -- see features.py's own docstring. Recruiting composite and
returning-production features use THAT season's own numbers (same-season,
known pre-season/signing-day). SP+/PPA/drive-rate/situational-split/SOS
features are explicitly a PRIOR-season prior (one year old, never that
season's own end-of-season aggregate, to stay leakage-safe). And the Elo-
style power rating (power_rating.py) regresses 75% of the way back to a
neutral 1500 baseline at every season boundary (SEASON_REGRESSION = 0.75),
so a team's rating carries forward almost none of its multi-year identity.
A 2016 training row already describes 2016's team using 2016-appropriate
inputs, not some decade-stale roster snapshot.

THE REAL VERSION OF THE RISK: Ridge fits ONE linear coefficient per feature
across the whole blended 10-season pool, and XGBoost fits one tree ensemble
the same way. If the true relationship between a feature and margin has
genuinely shifted era to era (the transfer portal/NIL era changing how much
recruiting rank vs. portal activity matters, conference realignment
changing what mw_involved_flag/sos_diff even mean), a single fixed-window
fit can't tell eras apart -- older seasons' games could be quietly pulling
the fit AWAY from what best predicts recent games. This script tests that
directly.

METHOD: same walk-forward machinery as model_comparison.py (import, not
reimplement -- backtest._test_weeks(), model.load_training_frame(),
model.fit_margin_model(), xgboost_model.fit_xgboost_margin_model(),
model.predict_margin(), and model_comparison.py's own _grade_side()/
summarize() so every number here is graded and reported exactly the way
Cole already reads model_comparison.py's output). For each RECENT test
week, two training sets are built from the exact same walk-forward cutoff
(model.load_training_frame(con, before_date=wk.week_start), i.e. everything
strictly before that week's own kickoffs -- no peeking):
  "full"   -- every season in the DB (today's behavior, unchanged)
  "recent" -- only the last --recent-seasons seasons (default 4, i.e. what
              Cole described as "last 3-4 seasons")
Both Ridge and XGBoost are refit fresh under each training set, predict the
same test week, and get graded through the SAME grade_spread_pick() call
every other script in this project uses, so "full" vs. "recent" is compared
apples-to-apples on identical test games.

SCOPE, ON PURPOSE: this only evaluates the last --test-seasons seasons
(default 2) of test weeks, not the full 10-year sweep model_comparison.py
walks -- the whole question here is "which training strategy predicts
TODAY's games better," not a full historical retrospective, and narrowing
the test set keeps this affordable to run locally. It still refits 4
models per test week (Ridge x2 windows, XGBoost x2 windows, one of which is
a real hyperparameter search) instead of model_comparison.py's 2 (Ridge x1,
XGBoost x1) -- but over roughly a fifth as many test weeks, so total
runtime should land in the same ballpark as a normal model_comparison.py
run, not meaningfully more.

Standalone diagnostic, same category as diagnose_spread_bias.py --
NOT wired into run_pipeline.py or weekly_pipeline.yml, and doesn't write
any CSV/JSON of its own. Console report only.

Usage:
    source .venv/bin/activate
    python src/diagnose_training_window.py
    python src/diagnose_training_window.py --recent-seasons 3 --test-seasons 2
"""
import argparse
import time

import duckdb
import pandas as pd

from config import DB_PATH
import model
import xgboost_model
from teams import MW_TEAMS_2026
from backtest import _test_weeks, EDGE_THRESHOLD, MIN_TRAIN_GAMES, grade_spread_pick
from model_comparison import _grade_side, summarize


def run(con, recent_seasons=4, test_seasons=2, edge_threshold=EDGE_THRESHOLD, min_train_games=MIN_TRAIN_GAMES):
    all_weeks = _test_weeks(con)
    max_season = int(all_weeks["season"].max())
    first_test_season = max_season - test_seasons + 1
    test_weeks = all_weeks[all_weeks["season"] >= first_test_season].reset_index(drop=True)
    full_pool = model.load_training_frame(con).set_index("game_id")

    print(f"Evaluating {len(test_weeks)} test week(s), season {first_test_season} through {max_season} -- "
          f"comparing the FULL-history training window (today's behavior) against a training window "
          f"restricted to the last {recent_seasons} season(s), refitting Ridge + XGBoost fresh for each "
          f"variant, per test week. Weeks with fewer than {min_train_games} training games under the "
          f"recent-window variant are skipped (full-history always has at least as many rows, so that's "
          f"the binding constraint).\n")

    results = []
    for idx, wk in enumerate(test_weeks.itertuples(), start=1):
        full_train = model.load_training_frame(con, before_date=wk.week_start)
        full_train = full_train.dropna(subset=["market_spread_home"])
        recent_train = full_train[full_train["season"] >= wk.season - recent_seasons + 1]

        if len(recent_train) < min_train_games:
            print(f"[{idx}/{len(test_weeks)}] {wk.season} week {wk.week}: skipped "
                  f"(only {len(recent_train)} games in the recent-window variant so far, "
                  f"need {min_train_games})")
            continue

        test_mask = (full_pool["season"] == wk.season) & (full_pool["week"] == wk.week)
        test_df = full_pool[test_mask].dropna(subset=["market_spread_home"])
        if test_df.empty:
            continue

        t0 = time.time()
        print(f"[{idx}/{len(test_weeks)}] {wk.season} week {wk.week}: full={len(full_train)} games, "
              f"recent={len(recent_train)} games, fitting Ridge + XGBoost under both windows, "
              f"testing {len(test_df)} game(s)...", flush=True)

        preds = {}
        for variant_label, train_df in (("full", full_train), ("recent", recent_train)):
            ridge_pipe, _ = model.fit_margin_model(train_df)
            preds[f"{variant_label}_ridge"] = model.predict_margin(ridge_pipe, test_df)
            xgb_pipe, _ = xgboost_model.fit_xgboost_margin_model(train_df)
            preds[f"{variant_label}_xgb"] = model.predict_margin(xgb_pipe, test_df)

        for i, (game_id, row) in enumerate(test_df.iterrows()):
            market_close = row["market_spread_home"]
            actual_margin = row["margin"]
            rec = {
                "game_id": game_id, "season": row["season"], "week": row["week"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "is_mw_game": row["home_team"] in MW_TEAMS_2026 or row["away_team"] in MW_TEAMS_2026,
                "market_spread_home": market_close, "actual_margin": actual_margin,
            }
            for variant_key in ("full_ridge", "full_xgb", "recent_ridge", "recent_xgb"):
                pred_margin = preds[variant_key][i]
                spread_home = -pred_margin  # margin -> spread convention, same as model.margin_to_model_spread_home
                edge, lean, is_bet, bet_result = _grade_side(spread_home, market_close, actual_margin, edge_threshold)
                rec[f"{variant_key}_spread_home"] = spread_home
                rec[f"{variant_key}_edge"] = edge
                rec[f"{variant_key}_lean"] = lean
                rec[f"{variant_key}_is_bet"] = is_bet
                rec[f"{variant_key}_result"] = bet_result
            results.append(rec)

        print(f"    done in {time.time() - t0:.0f}s", flush=True)

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recent-seasons", type=int, default=4,
                         help="How many seasons back the 'recent' training window includes (default 4).")
    parser.add_argument("--test-seasons", type=int, default=2,
                         help="How many of the most recent seasons to evaluate test weeks from (default 2).")
    args = parser.parse_args()

    con = duckdb.connect(str(DB_PATH), read_only=True)
    df = run(con, recent_seasons=args.recent_seasons, test_seasons=args.test_seasons)
    con.close()

    if df.empty:
        print("diagnose_training_window: not enough graded games yet in the requested test window.")
        return

    slices = [("Overall (all FBS)", df), ("Mountain West-involved", df[df["is_mw_game"]])]
    for label, sl in slices:
        print(f"=== {label} ({len(sl)} games) ===")
        for prefix, name in [
            ("full_ridge", "RIDGE, full 10-season window (today's behavior)"),
            ("recent_ridge", f"RIDGE, last {args.recent_seasons}-season window"),
            ("full_xgb", "XGBOOST, full 10-season window (today's behavior)"),
            ("recent_xgb", f"XGBOOST, last {args.recent_seasons}-season window"),
        ]:
            s = summarize(sl, prefix, label)
            print(f"--- {name} ---")
            for k, v in s.items():
                if k not in ("slice", "model"):
                    print(f"  {k}: {v}")
        print()

    print(
        "Reading this: 'ats_win_rate'/'roi_flat_stake' are graded on EVERY game (full record, same "
        "convention as backtest.py/model_comparison.py), 'real_edge_win_rate' is the subset that cleared "
        "the real betting edge threshold. If the recent-window numbers beat the full-window numbers here, "
        "that's real evidence the 10-season blend is diluting recent-era signal. If they're a wash (or "
        "full-window wins), the extra decade of games is pulling its weight as more training data without "
        "meaningfully stale-ing the fit -- consistent with every feature already being season-scoped "
        "rather than a raw multi-year roster snapshot (see this script's own docstring)."
    )


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
