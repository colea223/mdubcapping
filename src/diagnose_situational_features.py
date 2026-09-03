"""
Follow-up to the down/distance situational-splits feature engineering
(std_down_ppa_diff/passing_down_ppa_diff/red_zone_ppa_diff/explosive_rate_diff
-- see db/schema.sql's situational_stats_snapshots comment and model.py's own
docstring). A real backtest.py A/B (these 4 features ON vs OFF) showed a
small, consistent improvement across the whole FBS pool but a small,
consistent REGRESSION specifically on Mountain West-involved games -- ATS
win rate, ROI, Brier, AND CLV all moved the wrong way for MW games when the
features were added, even though the same 4 features helped everywhere else.
This script digs into WHY, testing three specific hypotheses rather than
just re-confirming the top-line numbers:

  1. COVERAGE: is the underlying play-by-play data CFBD has for MW teams
     thinner than for P4 teams? If MW games get less complete play-tracking
     (fewer nationally-televised/fully-charted games), off_plays/def_plays
     in situational_stats_snapshots -- the sample size each situational PPA
     average is actually computed from -- would be smaller for MW teams,
     making their situational splits noisier inputs than the same features
     are for a P4 team, even though the FEATURE itself is identical code.

  2. IMPUTATION-RATE DISPARITY: SimpleImputer(strategy="median") fills any
     missing situational diff with the GLOBAL median (computed across the
     whole P4-heavy training pool). If MW-involved rows are missing these
     values at a higher rate than the rest of the pool, a
     disproportionate share of MW training rows get fed a P4-flavored
     constant instead of real signal -- which would look exactly like "this
     feature is fine everywhere except MW."

  3. COEFFICIENT/MULTICOLLINEARITY: the 4 new features are all built from
     the same per-play PPA data that already feeds ppa_diff (the
     season-aggregate net PPA feature). If Ridge ends up putting real
     weight on the new, noisier splits at the expense of shrinking ppa_diff's
     own (cleaner, larger-sample) coefficient, that's a net loss of signal
     specifically wherever the splits are noisiest -- which, per hypothesis
     1, would be MW games.

Section 4 then reruns the full walk-forward backtest twice in one process
(toggling model.FEATURE_COLS in memory between runs -- both
model.fit_margin_model()/predict_margin() and backtest.run_backtest() look
up FEATURE_COLS from model's own module namespace at call time, so
reassigning model.FEATURE_COLS here before each call is enough; no file
edits or separate script runs needed) and prints the individual MW-involved
games where the model's spread pick moved the most, so the aggregate
regression can be traced back to specific real games rather than staying an
abstract stat.

Usage:
    source .venv/bin/activate
    python src/diagnose_situational_features.py
"""
import duckdb
import numpy as np
import pandas as pd
import time

from config import DB_PATH
import model
from backtest import run_backtest, summarize
from teams import MW_TEAMS_2026

SITUATIONAL_COLS = [
    "std_down_ppa_diff", "passing_down_ppa_diff", "red_zone_ppa_diff", "explosive_rate_diff",
]


def _reduced_and_full_cols():
    """
    (reduced_cols, full_cols) computed safely regardless of whether
    model.py's own FEATURE_COLS currently has the 4 situational columns in
    it or not -- NOT `list(model.FEATURE_COLS) + SITUATIONAL_COLS`, which
    silently double-adds them (and crashes downstream with a "duplicate
    column name" error from the imputer) the moment someone re-enables
    those 4 lines in model.py itself, which is exactly what happened after
    the FCS-contamination fix landed. Stripping SITUATIONAL_COLS out first
    and re-adding it makes this correct either way.
    """
    reduced = [c for c in model.FEATURE_COLS if c not in SITUATIONAL_COLS]
    return reduced, reduced + SITUATIONAL_COLS


def section1_coverage(con):
    print("=" * 78)
    print("1. Play-count coverage in situational_stats_snapshots: MW teams vs. everyone else")
    print("   (last as_of_week of each season -- i.e. that whole season's final sample size)")
    print("=" * 78)
    df = con.execute("""
        SELECT ss.season, ss.team, ss.off_plays, ss.def_plays
        FROM situational_stats_snapshots ss
        INNER JOIN (
            SELECT season, team, MAX(as_of_week) AS max_week
            FROM situational_stats_snapshots GROUP BY season, team
        ) mx ON mx.season = ss.season AND mx.team = ss.team AND mx.max_week = ss.as_of_week
    """).fetchdf()
    if df.empty:
        print("  No situational_stats_snapshots data yet -- run build_db.py first.\n")
        return
    df["is_mw"] = df["team"].isin(MW_TEAMS_2026)
    for label, sub in [("Mountain West teams", df[df["is_mw"]]), ("All other FBS teams", df[~df["is_mw"]])]:
        if sub.empty:
            print(f"  {label:<24} n=0")
            continue
        print(f"  {label:<24} n_team_seasons={len(sub):>5}  "
              f"mean_off_plays={sub['off_plays'].mean():>6.1f}  median_off_plays={sub['off_plays'].median():>6.1f}  "
              f"mean_def_plays={sub['def_plays'].mean():>6.1f}  median_def_plays={sub['def_plays'].median():>6.1f}")
    print()


def section2_imputation_rate(train_df):
    print("=" * 78)
    print("2. Missing-data rate for the 4 situational diff features: MW-involved games vs. others")
    print("   (a NULL here gets filled with the GLOBAL median by SimpleImputer -- if MW games are")
    print("    missing at a higher rate, they're disproportionately fed a P4-flavored constant)")
    print("=" * 78)
    is_mw = train_df["home_team"].isin(MW_TEAMS_2026) | train_df["away_team"].isin(MW_TEAMS_2026)
    for col in SITUATIONAL_COLS:
        mw_missing = train_df.loc[is_mw, col].isna().mean()
        other_missing = train_df.loc[~is_mw, col].isna().mean()
        print(f"  {col:<24} MW missing={mw_missing * 100:>5.1f}%   other missing={other_missing * 100:>5.1f}%   "
              f"(n_mw={int(is_mw.sum())}, n_other={int((~is_mw).sum())})")
    print()


def section3_coefficients(train_df):
    print("=" * 78)
    print("3. Ridge coefficients (standardized) with all 4 situational features ON")
    print("   -- fit once on the full completed-games pool, sorted by |coefficient|")
    print("=" * 78)
    _, full_cols = _reduced_and_full_cols()
    saved_cols = model.FEATURE_COLS
    model.FEATURE_COLS = full_cols
    try:
        pipe, _ = model.fit_margin_model(train_df)
    finally:
        model.FEATURE_COLS = saved_cols
    # NOT a plain zip(full_cols, coefs) -- SimpleImputer silently DROPS any
    # column that's entirely NaN in the training data ("Skipping features
    # without any observed values", the exact warning model.py's own
    # warnings.filterwarnings() call suppresses), so pipe.named_steps["ridge"]
    # .coef_ can come back SHORTER than full_cols. A bare zip() would then
    # silently pair each coefficient with the WRONG feature name instead of
    # erroring. get_feature_names_out() reports which columns actually
    # survived (in order), so pairing against THAT is correct regardless.
    surviving_cols = list(pipe.named_steps["impute"].get_feature_names_out())
    coefs = pipe.named_steps["ridge"].coef_
    dropped = [c for c in full_cols if c not in surviving_cols]
    if dropped:
        print(f"  NOTE: {len(dropped)} feature(s) had NO observed values in this training pool and were "
              f"dropped entirely (not shown below): {dropped}\n")
    alpha = pipe.named_steps["ridge"].alpha_
    ranked = sorted(zip(surviving_cols, coefs), key=lambda kv: -abs(kv[1]))
    for name, coef in ranked:
        flag = "  <-- new (situational)" if name in SITUATIONAL_COLS else ""
        print(f"  {name:<24} {coef:+.3f}{flag}")
    print(f"\n  (alpha selected by RidgeCV: {alpha:.3f})")
    print()


def section4_backtest_ab(con):
    print("=" * 78)
    print("4. Full walk-forward backtest, WITH vs. WITHOUT the 4 situational features")
    print("   (re-run twice in this one process -- current model.py state is restored after)")
    print("=" * 78)
    saved_cols = model.FEATURE_COLS
    reduced_cols, full_cols = _reduced_and_full_cols()

    model.FEATURE_COLS = full_cols
    t0 = time.time()
    df_with = run_backtest(con)
    print(f"  backtest WITH situational features: {time.time() - t0:.1f}s")

    model.FEATURE_COLS = reduced_cols
    t0 = time.time()
    df_without = run_backtest(con)
    print(f"  backtest WITHOUT situational features: {time.time() - t0:.1f}s")

    model.FEATURE_COLS = saved_cols  # restore -- don't leave the module mutated for anything after this
    print()

    mw_with = df_with[df_with["is_mw_game"]]
    mw_without = df_without[df_without["is_mw_game"]]
    print("  MW-involved spread summary, WITH:   ", summarize(mw_with, "MW, with"))
    print("  MW-involved spread summary, WITHOUT:", summarize(mw_without, "MW, without"))
    print()

    # Per-game comparison -- both runs cover the exact same games (same
    # walk-forward test weeks), so an inner merge on game_id lines up each
    # game's two spread picks directly.
    merged = df_with[["game_id", "season", "week", "home_team", "away_team", "market_spread_home",
                       "model_spread_home", "actual_margin", "lean", "bet_result"]].merge(
        df_without[["game_id", "model_spread_home", "lean", "bet_result"]],
        on="game_id", suffixes=("_with", "_without"),
    )
    merged = merged[merged["home_team"].isin(MW_TEAMS_2026) | merged["away_team"].isin(MW_TEAMS_2026)]
    merged["pick_swing"] = (merged["model_spread_home_with"] - merged["model_spread_home_without"]).abs()
    merged["flipped_lean"] = merged["lean_with"] != merged["lean_without"]

    print(f"  {int(merged['flipped_lean'].sum())} of {len(merged)} MW-involved games flipped which side "
          f"the model leaned toward when the situational features were added/removed.\n")

    print("  Biggest individual movers (MW-involved games, sorted by |spread swing|):")
    top = merged.sort_values("pick_swing", ascending=False).head(15)
    for r in top.itertuples():
        print(f"    {r.season} wk{r.week:<2} {r.away_team} @ {r.home_team:<20} "
              f"market={r.market_spread_home:+.1f}  actual_margin={r.actual_margin:+.1f}  "
              f"WITH={r.model_spread_home_with:+.1f} ({r.bet_result_with or '--'})  "
              f"WITHOUT={r.model_spread_home_without:+.1f} ({r.bet_result_without or '--'})  "
              f"swing={r.pick_swing:.1f}{'  [FLIPPED SIDE]' if r.flipped_lean else ''}")
    print()


def main():
    con = duckdb.connect(str(DB_PATH))
    train_df = model.load_training_frame(con)
    if train_df.empty:
        print("diagnose: no training data yet -- run the full pipeline first.")
        con.close()
        return

    section1_coverage(con)
    section2_imputation_rate(train_df)
    section3_coefficients(train_df)
    section4_backtest_ab(con)

    con.close()


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
