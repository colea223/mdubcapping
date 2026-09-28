"""
Validation harness for the newest candidate feature -- sor_diff (Strength
of Record, home minus away). Computed and stored in game_features (see
features.py's own docstring and schema.sql's sor_baseline comment), but
deliberately NOT yet added to model.FEATURE_COLS -- same discipline this
project already applied to the down/distance situational splits
(diagnose_situational_features.py) and the sos_diff/returning_production_diff/
qb_continuity_diff trio (diagnose_new_features.py) before trusting either:
prove it helps in a real walk-forward backtest first, on real data, before
touching model.py.

Context for why this diagnostic exists right now: Cole flagged that North
Dakota State and James Madison look too high (rank #8/#9) on the raw Elo
baseline (power_rating.current_ratings(), surfaced on the Team Profiles
tab) and asked whether SOS or SOR would be a good adjustment. A direct
look at both teams' numbers already answered the "is this a bug" half of
that question (see the diagnose_training_window.py-style investigation
done in chat, not in a script) -- SOR itself still ranks both teams highly
(NDSU #12, JMU #7 of 233 by SOR this season) because they're genuinely
undefeated against decent-enough competition, so SOR isn't a "fix" for
that specific number. This script asks the SEPARATE, real question: does
sor_diff, as a MODEL FEATURE, actually improve Ridge's real walk-forward
ATS accuracy across the FULL history -- the only basis this project ever
uses to decide whether a candidate feature earns a permanent seat in
model.FEATURE_COLS.

Section 1 checks coverage (sor_diff is computed for every game the same
season it happens, so this should be ~100% for every season with games --
unlike sos_diff/returning_production_diff, there's no "how far back does
this cover" question here).

Section 2 is the real test: 2 walk-forward backtests in one process (same
in-memory model.FEATURE_COLS toggle trick diagnose_new_features.py uses --
fit_margin_model()/predict_margin()/run_backtest() all look up
FEATURE_COLS from model's own module namespace at call time):
  (a) baseline: current FEATURE_COLS, no sor_diff
  (b) baseline + sor_diff
-- compared overall AND on the Mountain West-involved slice (same reason
diagnose_new_features.py checks both: a feature can look fine overall while
doing something different specifically to MW games, given how thin that
sample is against the rest of the P4-heavy pool).

Usage:
    source .venv/bin/activate
    python src/power_rating.py   # must run first -- writes sor_baseline
    python src/features.py       # must run second -- writes sor_diff
    python src/diagnose_sor_feature.py
"""
import duckdb
import time

from config import DB_PATH
import model
from backtest import run_backtest, summarize

NEW_COL = "sor_diff"

VARIANTS = [
    ("baseline (no sor_diff)", []),
    ("+ sor_diff", [NEW_COL]),
]


def section1_coverage(train_df):
    print("=" * 78)
    print("1. Coverage: how many training rows have sor_diff, and since when")
    print("=" * 78)
    non_null = train_df[train_df[NEW_COL].notna()]
    if non_null.empty:
        print(f"  {NEW_COL:<12} 0 rows -- all-NULL (table empty or not yet built?)")
    else:
        pct = len(non_null) / len(train_df) * 100
        print(f"  {NEW_COL:<12} {len(non_null):>5} / {len(train_df)} rows ({pct:4.1f}%), "
              f"seasons {int(non_null['season'].min())}-{int(non_null['season'].max())}")
    print()


def section2_backtest_variants(con):
    print("=" * 78)
    print("2. Walk-forward backtest: baseline vs. + sor_diff, overall + MW-involved")
    print("   (2 backtests in this one process -- current model.py state is restored after)")
    print("=" * 78)
    saved_cols = model.FEATURE_COLS
    base_cols = [c for c in model.FEATURE_COLS if c != NEW_COL]

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
        print("diagnose_sor_feature: no training data yet -- run the full pipeline first.")
        con.close()
        return
    if NEW_COL not in train_df.columns:
        print(f"diagnose_sor_feature: {NEW_COL} not found in game_features -- rerun "
              f"src/power_rating.py, then src/features.py to pick up today's schema changes.")
        con.close()
        return

    section1_coverage(train_df)
    section2_backtest_variants(con)

    con.close()


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
