"""
Validation harness for the 3 newest candidate features -- sos_diff (prior-
season strength of schedule), returning_production_diff (this season's
overall returning-production %, the "team composite strength" signal), and
qb_continuity_diff (this season's passing-game returning-production %, the
best available QB-continuity proxy). All 3 are already computed and stored
in game_features (see features.py's own docstring and schema.sql's
returning_production/sos_ratings comments), but deliberately NOT yet added
to model.FEATURE_COLS -- same discipline this project applied to the
down/distance situational splits (diagnose_situational_features.py) before
trusting those: prove it helps in a real walk-forward backtest first.

Section 1 checks coverage -- how far back sos_ratings/returning_production
actually go, since returning_production in particular is unlikely to cover
this project's full 2016+ history the same way advanced_stats/sp_ratings do.
A feature that's only non-null for the last few seasons still HELPS the
model in the weeks it fires (the imputer fills the rest with the training
pool's median), but it's worth knowing before reading too much into an
early-season game's backtest row.

Section 2 is the real test: 5 walk-forward backtests in one process (same
in-memory model.FEATURE_COLS toggle trick as diagnose_situational_features.py
-- fit_margin_model()/predict_margin()/run_backtest() all look up
FEATURE_COLS from model's own module namespace at call time) --
  (a) baseline: current FEATURE_COLS, none of the 3 new features
  (b) baseline + sos_diff only
  (c) baseline + returning_production_diff only
  (d) baseline + qb_continuity_diff only
  (e) baseline + all 3 together
-- each compared to (a), overall AND on the Mountain West-involved slice
(the situational-splits saga is a reminder that a feature can look fine
overall while doing something different specifically to MW games, given how
thin the MW-specific sample is against the rest of the P4-heavy pool).

Usage:
    source .venv/bin/activate
    python src/diagnose_new_features.py
"""
import duckdb
import pandas as pd
import time

from config import DB_PATH
import model
from backtest import run_backtest, summarize
from teams import MW_TEAMS_2026

NEW_COLS = ["sos_diff", "returning_production_diff", "qb_continuity_diff"]

VARIANTS = [
    ("baseline (no new features)", []),
    ("+ sos_diff", ["sos_diff"]),
    ("+ returning_production_diff", ["returning_production_diff"]),
    ("+ qb_continuity_diff", ["qb_continuity_diff"]),
    ("+ all 3 together", NEW_COLS),
]


def section1_coverage(train_df):
    print("=" * 78)
    print("1. Coverage: how many training rows have each new feature, and since when")
    print("=" * 78)
    for col in NEW_COLS:
        non_null = train_df[train_df[col].notna()]
        if non_null.empty:
            print(f"  {col:<28} 0 rows -- all-NULL (table empty or not yet built?)")
            continue
        pct = len(non_null) / len(train_df) * 100
        print(f"  {col:<28} {len(non_null):>5} / {len(train_df)} rows ({pct:4.1f}%), "
              f"seasons {int(non_null['season'].min())}-{int(non_null['season'].max())}")
    print()


def section2_backtest_variants(con):
    print("=" * 78)
    print("2. Walk-forward backtest: baseline vs. each candidate feature, overall + MW-involved")
    print("   (5 backtests in this one process -- current model.py state is restored after)")
    print("=" * 78)
    saved_cols = model.FEATURE_COLS
    base_cols = [c for c in model.FEATURE_COLS if c not in NEW_COLS]

    results = {}
    try:
        for label, extra_cols in VARIANTS:
            model.FEATURE_COLS = base_cols + extra_cols
            t0 = time.time()
            df = run_backtest(con)
            print(f"  ran '{label}': {time.time() - t0:.1f}s ({len(df)} graded games)")
            results[label] = df
    finally:
        model.FEATURE_COLS = saved_cols  # restore -- don't leave the module mutated for anything after this
    print()

    baseline_label = VARIANTS[0][0]
    baseline_df = results[baseline_label]
    if baseline_df.empty:
        print("  No graded games in any variant -- not enough completed games with a market line yet "
              "(need MIN_TRAIN_GAMES worth of history before backtest.py grades anything). "
              "Nothing to compare.\n")
        return
    baseline_overall = summarize(baseline_df, "overall")
    baseline_mw = summarize(baseline_df[baseline_df["is_mw_game"]], "MW")
    print(f"  {baseline_label}")
    print(f"    overall: {baseline_overall}")
    print(f"    MW:      {baseline_mw}")
    print()

    for label, _ in VARIANTS[1:]:
        df = results[label]
        if df.empty:
            print(f"  {label}\n    (no graded games)\n")
            continue
        overall = summarize(df, "overall")
        mw = summarize(df[df["is_mw_game"]], "MW")
        print(f"  {label}")
        print(f"    overall: {overall}")
        print(f"    MW:      {mw}")

        merged = baseline_df[["game_id", "lean"]].merge(
            df[["game_id", "lean"]], on="game_id", suffixes=("_base", "_new"),
        )
        flipped = (merged["lean_base"] != merged["lean_new"]).sum()
        print(f"    {flipped} of {len(merged)} games flipped which side the model leaned toward vs. baseline")
        print()


def main():
    con = duckdb.connect(str(DB_PATH))
    train_df = model.load_training_frame(con)
    if train_df.empty:
        print("diagnose_new_features: no training data yet -- run the full pipeline first.")
        con.close()
        return
    missing = [c for c in NEW_COLS if c not in train_df.columns]
    if missing:
        print(f"diagnose_new_features: {missing} not found in game_features -- rerun "
              f"src/build_db.py, src/power_rating.py, then src/features.py to pick up today's schema changes.")
        con.close()
        return

    section1_coverage(train_df)
    section2_backtest_variants(con)

    con.close()


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
