"""
Calibration check for the moneyline win-probability conversion --
model.margin_to_home_win_prob(pred_margin, residual_std), which is just a
Gaussian CDF: norm.cdf(pred_margin / residual_std), using ONE FIXED
residual_std for every game in a given backtest fold. This script asks the
question that matters before touching that function at all: is it actually
well-calibrated, or does it just look reasonable in aggregate?

WHY THIS MATTERS FOR MONEYLINE SPECIFICALLY: the spread/total markets only
care about the SIGN of (model line - market line) -- calibration quality is
secondary there. Moneyline is graded directly against a probability
(ml_edge = home_win_prob - market's own no-vig probability, see
backtest.py's run_backtest()), so if home_win_prob is systematically off in
some region (e.g. overconfident on big favorites, underconfident on
close games), that shows up as real, repeatable moneyline losses -- not
just noise.

Section 1: reliability table. Buckets every graded game by its predicted
home_win_prob into deciles, and compares the bucket's MEAN predicted
probability against the ACTUAL observed home-win rate within that bucket.
A well-calibrated model has these nearly equal in every bucket (points on
the diagonal, in reliability-diagram terms). A gap tells you not just THAT
there's a problem but WHERE -- e.g. "the model says 80-90% and reality is
70%" specifically flags overconfidence on favorites, which a single overall
Brier score can't localize.

Section 2: the existing moneyline betting summary (backtest.py's
summarize_moneyline()) for context -- real win rate/ROI/Brier on the games
that actually cleared the edge threshold, overall and MW-involved.

Section 3: an ILLUSTRATIVE isotonic recalibration -- fits a monotonic
mapping from pred_margin to actual outcome on this SAME backtest data and
reports what Brier score it would have achieved. This is explicitly NOT a
walk-forward-safe test (it's fit and evaluated on the same games, which is
circular/in-sample) -- it exists only to answer "is there enough
miscalibration here that a real fix is worth building," not as a result to
trust or ship. If this shows a meaningful gap, the next step is a PROPER
walk-forward version (refit the calibration each test week using only prior
weeks, same discipline as every other feature in this project) before
touching model.py for real.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python src/diagnose_ml_calibration.py
"""
import time

import duckdb
import numpy as np
import pandas as pd

from config import DB_PATH
from backtest import run_backtest, summarize_moneyline

N_BUCKETS = 10


def section1_reliability(df, label):
    print(f"--- Reliability table: {label} ---")
    graded = df.dropna(subset=["home_win_prob", "actual_home_win"])
    # Ties (actual_home_win == 0.5) are essentially nonexistent in CFB
    # (no regulation ties, OT always produces a winner) -- drop the rare/
    # nonexistent case rather than let it muddy a bucket's win rate.
    graded = graded[graded["actual_home_win"] != 0.5]
    if graded.empty:
        print("  no graded games with a win probability -- nothing to check\n")
        return
    graded = graded.copy()
    graded["bucket"] = pd.cut(graded["home_win_prob"], bins=np.linspace(0, 1, N_BUCKETS + 1), include_lowest=True)
    table = graded.groupby("bucket", observed=True).agg(
        n=("actual_home_win", "size"),
        mean_pred=("home_win_prob", "mean"),
        actual_rate=("actual_home_win", "mean"),
    )
    print(f"  {'bucket':<16} {'n':>6} {'mean pred':>10} {'actual':>8} {'gap':>8}")
    max_gap = 0.0
    for bucket, row in table.iterrows():
        gap = row["actual_rate"] - row["mean_pred"]
        max_gap = max(max_gap, abs(gap))
        flag = "  <-- overconfident" if gap < -0.05 and row["n"] >= 10 else (
            "  <-- underconfident" if gap > 0.05 and row["n"] >= 10 else "")
        print(f"  {str(bucket):<16} {int(row['n']):>6} {row['mean_pred']:>10.3f} "
              f"{row['actual_rate']:>8.3f} {gap:>+8.3f}{flag}")
    print(f"  Largest bucket gap: {max_gap:.3f} "
          f"({'meaningful, worth a real fix' if max_gap > 0.07 else 'small, likely just bucket noise'})\n")


def section3_illustrative_recalibration(df, label):
    print(f"--- Section 3 (illustrative only, NOT walk-forward-safe): {label} ---")
    graded = df.dropna(subset=["home_win_prob", "actual_home_win", "model_spread_home"])
    graded = graded[graded["actual_home_win"] != 0.5]
    if len(graded) < 50:
        print("  too few graded games to fit a meaningful calibration curve -- skipping\n")
        return
    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        print("  scikit-learn not available -- skipping\n")
        return

    pred_margin = -graded["model_spread_home"].to_numpy()  # spread_home = -pred_margin, see model.py
    actual = graded["actual_home_win"].to_numpy()
    current_brier = float(np.mean((graded["home_win_prob"] - actual) ** 2))

    iso = IsotonicRegression(out_of_bounds="clip")
    recalibrated = iso.fit_transform(pred_margin, actual)
    recal_brier = float(np.mean((recalibrated - actual) ** 2))

    print(f"  current (Gaussian) Brier score:      {current_brier:.4f}")
    print(f"  in-sample recalibrated Brier score:  {recal_brier:.4f}  (best case, NOT a real out-of-sample number)")
    improvement = current_brier - recal_brier
    print(f"  gap: {improvement:+.4f} "
          f"({'worth building a real walk-forward version' if improvement > 0.003 else 'small -- probably not worth the added complexity'})\n")


def main():
    con = duckdb.connect(str(DB_PATH))
    t0 = time.time()
    df = run_backtest(con)
    print(f"Backtest ran in {time.time() - t0:.1f}s ({len(df)} graded games)\n")
    con.close()

    if df.empty:
        print("diagnose_ml_calibration: no graded games -- run the full pipeline first.")
        return

    mw_df = df[df["is_mw_game"]]

    print("=" * 78)
    section1_reliability(df, "overall")
    section1_reliability(mw_df, "Mountain West-involved")

    print("=" * 78)
    print("--- Existing moneyline betting summary, for context ---")
    print(f"  overall: {summarize_moneyline(df, 'overall')}")
    print(f"  MW:      {summarize_moneyline(mw_df, 'MW')}")
    print()

    print("=" * 78)
    section3_illustrative_recalibration(df, "overall")
    section3_illustrative_recalibration(mw_df, "Mountain West-involved")


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
