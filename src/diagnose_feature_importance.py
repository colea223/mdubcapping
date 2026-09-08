"""
Read-only diagnostic answering "would Lasso (or some other importance check)
tell us anything useful about FEATURE_COLS" -- run this on a machine with the
FULL raw-data pull (drive stats, situational splits, recruiting, PPA), since
a thin/partial data/raw/ will leave the newer engineered features entirely
null and make this report meaningless for them (see Section 1).

Three checks, in order:

1. Null-rate audit -- prints what fraction of FEATURE_COLS is actually
   populated across the full training set. Read this FIRST: if any of the
   newer features (drive_*, *_ppa_diff, explosive_rate_diff,
   returning_production_diff) show 100% null, everything below is only
   trustworthy for the older features that aren't -- don't read a Lasso/
   XGBoost result as "this feature doesn't matter" when it's actually just
   "this feature was never observed on this machine."

2. LassoCV (supervised, L1-regularized regression -- Ridge's sibling, same
   RidgeCV-style automatic alpha search) on the full standardized feature
   set, predicting home margin exactly like model.py's own RidgeCV does.
   Lasso's whole trick vs. Ridge is that L1 regularization can drive a
   coefficient to EXACTLY zero rather than just shrinking it -- so this is
   a fast way to see which features a straight-line model finds totally
   redundant given what else is already in the feature set. NOTE: "zeroed
   by Lasso" usually means "redundant with something else already in the
   model" (e.g. sp_diff overlapping rating_diff/ppa_diff), not "this signal
   doesn't exist" -- a feature can vanish here and still be the ONLY signal
   for its own information if the more-dominant correlated feature were
   removed.

3. XGBoost gain-based feature_importances_ on the same features, for
   comparison against Lasso. This project already runs XGBoost as the Dub
   Beta candidate, so this reuses that same signal rather than introducing
   a new model -- and it's a useful cross-check specifically because trees
   don't have Lasso's "zero out the redundant twin" behavior, they can
   split credit between correlated features instead. A feature ranked
   low by BOTH Lasso and XGBoost is a much stronger prune candidate than
   one only one method flagged.

4. A correlation matrix across the 4 team-quality proxies (rating_diff,
   sp_diff, ppa_diff, talent_diff) -- these all encode some version of "how
   good is this team," from different sources (this project's own Elo/
   Massey replay, CFBD's SP+, CFBD's PPA, 247/CFBD recruiting talent), so
   high pairwise correlation here is expected and is most of the reason
   Lasso zeroes one of them out rather than a sign anything is broken.

IMPORTANT -- what NOT to do with this: don't remove anything from
FEATURE_COLS just because it shows up weak here. This project's own
precedent (diagnose_new_features.py, diagnose_situational_features.py) is
that a feature only earns removal or addition after a real walk-forward
backtest shows it changes actual graded ATS win rate / ROI, both overall
and on the Mountain West slice specifically -- an in-sample linear
coefficient or a gain score is a cheap SCREEN for what to backtest next,
not a verdict on its own. Two of this project's 3 rejected feature
candidates (sos_diff, qb_continuity_diff) looked perfectly reasonable
going in; only the backtest told the real story.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python src/diagnose_feature_importance.py
"""
import warnings

warnings.filterwarnings("ignore")

import duckdb
import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LassoCV
from sklearn.preprocessing import StandardScaler

from config import DB_PATH
from model import FEATURE_COLS, load_training_frame


def section1_null_audit(df):
    print("=== 1. Null-rate audit (read this first) ===")
    print(f"Training rows: {len(df)}\n")
    any_fully_null = False
    for col in FEATURE_COLS:
        n_null = df[col].isna().sum()
        pct = 100 * n_null / len(df)
        flag = ""
        if n_null == len(df):
            flag = "  <-- ENTIRELY NULL -- this machine's raw data doesn't cover this feature at all"
            any_fully_null = True
        print(f"  {col:<28s} null: {n_null:5d} / {len(df)}  ({pct:5.1f}%){flag}")
    if any_fully_null:
        print(
            "\n  WARNING: at least one feature above is 100% null on this machine -- Sections "
            "2-4 below will still run (SimpleImputer/pandas just drop/median-fill those columns), "
            "but any result touching a fully-null feature is meaningless here specifically, not a "
            "real finding about that feature. Usually means data/raw/ is missing a snapshot pull "
            "(drive stats, situational splits, recruiting, or player_season_ppa) -- see features.py "
            "and build_db.py for which raw pull backs which column."
        )
    print()
    return any_fully_null


def section2_lasso(df):
    print("=== 2. LassoCV coefficients (standardized features, predicting home margin) ===")
    X, y = df[FEATURE_COLS], df["margin"]
    X_imp = SimpleImputer(strategy="median").fit_transform(X)
    X_scaled = StandardScaler().fit_transform(X_imp)

    lasso = LassoCV(cv=5, random_state=0, max_iter=20000, n_alphas=100)
    lasso.fit(X_scaled, y)

    coefs = sorted(zip(FEATURE_COLS, lasso.coef_), key=lambda kv: -abs(kv[1]))
    print(f"Chosen alpha (5-fold CV): {lasso.alpha_:.4f}\n")
    zeroed = []
    for name, c in coefs:
        if abs(c) < 1e-9:
            zeroed.append(name)
        print(f"  {name:<28s} {c: 10.4f}")
    print(f"\nZeroed out entirely by Lasso ({len(zeroed)}): {zeroed or 'none'}")
    print()
    return {name: c for name, c in coefs}


def section3_xgboost(df):
    print("=== 3. XGBoost gain-based importance (same features, for comparison) ===")
    try:
        from xgboost import XGBRegressor
    except ImportError:
        print("  xgboost not installed in this environment -- skipping (pip install xgboost).\n")
        return {}

    X, y = df[FEATURE_COLS], df["margin"]
    X_imp = SimpleImputer(strategy="median").fit_transform(X)

    model = XGBRegressor(n_estimators=300, max_depth=3, learning_rate=0.05, random_state=0)
    model.fit(X_imp, y)
    gains = sorted(zip(FEATURE_COLS, model.feature_importances_), key=lambda kv: -kv[1])
    for name, g in gains:
        print(f"  {name:<28s} {g:.4f}")
    print()
    return {name: g for name, g in gains}


def section4_correlation(df):
    print("=== 4. Correlation among the 4 team-quality proxies ===")
    quality_cols = [c for c in ["rating_diff", "sp_diff", "ppa_diff", "talent_diff"] if c in df.columns]
    print(df[quality_cols].corr().round(2))
    print(
        "\n(High pairwise correlation here is expected -- all 4 encode 'how good is this team' "
        "from a different source. This is most of why Lasso zeroes one of them out above, not a "
        "sign anything is broken.)\n"
    )


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    df = load_training_frame(con)
    con.close()

    any_fully_null = section1_null_audit(df)
    lasso_coefs = section2_lasso(df)
    xgb_gains = section3_xgboost(df)
    section4_correlation(df)

    print("=== Cross-check: features weak in BOTH Lasso and XGBoost (strongest prune candidates) ===")
    if lasso_coefs and xgb_gains:
        xgb_median = float(np.median(list(xgb_gains.values())))
        weak_both = [
            name for name in FEATURE_COLS
            if abs(lasso_coefs.get(name, 0)) < 1e-9 and xgb_gains.get(name, 1.0) < xgb_median
        ]
        print(f"  {weak_both or 'none'}")
    if any_fully_null:
        print(
            "\n  (Some FEATURE_COLS were entirely null on this run -- re-run after confirming "
            "data/raw/ actually covers those features before trusting this cross-check list.)"
        )
    print(
        "\nReminder: treat anything above as a SCREEN, not a verdict -- confirm any actual "
        "FEATURE_COLS change with a real walk-forward backtest (same pattern as "
        "diagnose_new_features.py) before touching model.py."
    )


if __name__ == "__main__":
    main()
