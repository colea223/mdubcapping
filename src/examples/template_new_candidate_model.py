"""
TEMPLATE -- how to add a brand-new candidate margin-of-victory model,
following the exact pattern model.py (Ridge) and xgboost_model.py (XGBoost)
already use. Copy this file into src/ under its own name (e.g.
elasticnet_model.py), then follow the three checkboxes below.

This particular example is a REAL, useful candidate, not a toy: ElasticNet
is Ridge's L1+L2 cousin -- unlike Ridge, which shrinks every feature's
coefficient toward zero but never all the way to exactly zero, ElasticNet's
L1 term CAN zero a weak feature's coefficient out entirely. Comparing this
against Ridge in model_comparison.py-style walk-forward grading is a cheap,
real way to answer "are any of our ~20 FEATURE_COLS just noise?" -- if
ElasticNet consistently zeroes the same feature out across many walk-
forward refits, that's real evidence for dropping it (see
diagnose_feature_value.py's sibling template for a more direct version of
that same question).

No new pip installs needed -- ElasticNetCV ships with the scikit-learn
this project already depends on.

THE THREE-STEP PATTERN EVERY NEW CANDIDATE FOLLOWS:
  1. Import model.FEATURE_COLS -- never redefine your own feature list.
     Different features would mean you're no longer comparing MODEL
     ARCHITECTURE against the existing candidates, you'd be comparing
     architecture-plus-features, which muddies the result (see
     xgboost_model.py's own docstring for this exact point).
  2. Expose fit_*_margin_model(train_df) -> (fitted_pipeline, residual_std)
     and let model.predict_margin(pipe, df) work on your pipeline unchanged
     (it just calls pipe.predict(df[FEATURE_COLS]) -- any sklearn-style
     pipeline with that method works, no special-casing needed anywhere
     else in the project).
  3. Wire it into model_comparison.py's run_comparison() the same way
     xgb_pipe/xgb_pred_margin already sit alongside ridge_pipe/ridge_pred_margin
     (see that function's loop) -- then it gets graded through the exact
     same grade_spread_pick() call as every other candidate, apples-to-
     apples, for free.

Usage (standalone smoke test):
    source .venv/bin/activate
    python src/examples/template_new_candidate_model.py
"""
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import ElasticNetCV
from sklearn.model_selection import cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from model import FEATURE_COLS  # noqa: F401 -- re-exported, same convention xgboost_model.py uses

# L1_RATIOS spans from "mostly Ridge" (0.1) to "mostly Lasso" (0.9) --
# ElasticNetCV cross-validates over both this grid AND its own internal
# alpha path in one call, so (unlike XGBoost's RandomizedSearchCV) there's
# no separate hyperparameter grid to hand-maintain here.
L1_RATIOS = [0.1, 0.3, 0.5, 0.7, 0.9, 0.95, 0.99]
N_ALPHAS = 50
RANDOM_STATE = 42


def build_elasticnet_pipeline() -> Pipeline:
    """
    Imputer + scaler, same as model.build_pipeline() -- StandardScaler is
    load-bearing here too (ElasticNet's penalty needs comparable feature
    scales, exactly like Ridge's does).
    """
    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("elasticnet", ElasticNetCV(
            l1_ratio=L1_RATIOS, alphas=N_ALPHAS, cv=5,
            random_state=RANDOM_STATE, max_iter=5000,
        )),
    ])


def fit_elasticnet_margin_model(train_df: pd.DataFrame):
    """
    Same (fitted_pipeline, residual_std) contract as model.fit_margin_model
    and xgboost_model.fit_xgboost_margin_model -- residual_std comes from
    5-fold out-of-fold predictions on the training set, never in-sample
    residuals (see model.fit_margin_model's own docstring for why).
    """
    X, y = train_df[FEATURE_COLS], train_df["margin"]
    n = len(train_df)
    pipe = build_elasticnet_pipeline()

    if n < 10:
        pipe.fit(X, y)
        resid = y - pipe.predict(X)
    else:
        n_folds = 5 if n >= 50 else max(2, min(5, n // 10))
        oof_pred = cross_val_predict(pipe, X, y, cv=n_folds)
        resid = y - oof_pred
        pipe.fit(X, y)  # final model trained on all training data

    residual_std = float(np.std(resid)) if len(resid) else 14.0
    return pipe, residual_std


def zeroed_features(pipe: Pipeline) -> list:
    """
    Convenience helper unique to ElasticNet (Ridge/XGBoost have no direct
    equivalent): which FEATURE_COLS did this fit's L1 term zero out
    entirely? A feature that's ~always zeroed across many walk-forward
    refits is a real, data-driven "this isn't pulling its weight" signal --
    see this file's own docstring.
    """
    coefs = pipe.named_steps["elasticnet"].coef_
    return [feat for feat, c in zip(FEATURE_COLS, coefs) if c == 0.0]


if __name__ == "__main__":
    # Standalone smoke test against whatever DB this is run against --
    # mirrors predict_week.py's own "train on everything you have, no
    # walk-forward cutoff" pattern, just to prove the fit runs end to end.
    import duckdb
    import model
    from config import DB_PATH

    con = duckdb.connect(str(DB_PATH), read_only=True)
    train_df = model.load_training_frame(con)
    con.close()

    print(f"Fitting ElasticNet on {len(train_df)} completed games...")
    pipe, residual_std = fit_elasticnet_margin_model(train_df)
    print(f"Residual std: {residual_std:.2f} pts (compare to Ridge's/XGBoost's own -- "
          f"lower is a tighter fit, but see this project's walk-forward backtest discipline "
          f"before reading anything into that alone).")

    zeroed = zeroed_features(pipe)
    if zeroed:
        print(f"L1 term zeroed out {len(zeroed)}/{len(FEATURE_COLS)} feature(s) entirely: {zeroed}")
    else:
        print("L1 term kept every feature -- nothing looked like pure noise on this fit.")
