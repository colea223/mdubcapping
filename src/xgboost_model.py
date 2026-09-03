"""
An alternate margin-of-victory model, sitting alongside model.py's Ridge --
NOT replacing it. Same target (home margin), same FEATURE_COLS (imported
straight from model.py, not redefined here), so the two are a clean
apples-to-apples comparison of MODEL ARCHITECTURE (regularized linear vs.
gradient-boosted trees), not a comparison muddied by also changing the
inputs. See model_comparison.py for the walk-forward harness that actually
grades the two against each other and against Vegas, and
excel/update_model_comparison_tab.py for where that lands: its own "Model
Comparison" tab in the tracker workbook, informational only.

predict_week.py also calls this module now, to produce a live "XGBoost Line
(Home)" for the upcoming week alongside Ridge's -- but only as an extra,
informational column in the predictions CSV that excel/update_tracker.py's
Weekly Slate never reads (see predict_week.py's own docstring). Nothing in
run_pipeline.py or the website calls this module directly, and Ridge stays
the one live/production model everywhere a real bet is actually placed,
until a real backtest earns XGBoost that spot.

TUNING: same "cross-validation, not hand-tuned" discipline model.py's
RidgeCV already applies to alpha -- XGBoost just has more knobs (tree depth,
learning rate, number of trees, subsampling, regularization), so hand-tuning
it would be a worse version of the exact mistake RidgeCV was chosen to avoid.
fit_xgboost_margin_model() runs a RandomizedSearchCV over a modest grid,
scored by k-fold cross-validation on the TRAINING set only (never the test
week being predicted -- same walk-forward discipline backtest.py enforces
everywhere else), and returns the best-scoring pipeline already refit on all
of the training data. Nobody needs to touch a dial.

Usage: not a standalone script -- imported by model_comparison.py the same
way model.py's fit_margin_model is.
"""
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.model_selection import RandomizedSearchCV, cross_val_predict
from sklearn.pipeline import Pipeline
from xgboost import XGBRegressor

from model import FEATURE_COLS  # noqa: F401 -- re-exported so callers only need one import

# Modest grid -- this is a few thousand rows of college football, not a
# Kaggle-scale dataset, so a huge search would mostly just find noise.
# max_depth capped at 5 and min_child_weight/reg_lambda included specifically
# to keep the search honest about overfitting risk on a dataset this size.
XGB_PARAM_GRID = {
    "xgb__n_estimators": [100, 150, 200, 300, 400],
    "xgb__max_depth": [2, 3, 4, 5],
    "xgb__learning_rate": [0.01, 0.03, 0.05, 0.08, 0.1],
    "xgb__subsample": [0.7, 0.85, 1.0],
    "xgb__colsample_bytree": [0.6, 0.8, 1.0],
    "xgb__min_child_weight": [1, 3, 5, 10],
    "xgb__reg_lambda": [1.0, 2.0, 5.0, 10.0],
}
DEFAULT_N_ITER = 30
RANDOM_STATE = 42


def build_xgb_pipeline(**xgb_params) -> Pipeline:
    """
    Imputer only -- no StandardScaler. Tree splits are scale-invariant, so
    scaling would be pure no-op busywork here (unlike Ridge, where it's load-
    bearing for the regularization penalty to treat features comparably).
    """
    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("xgb", XGBRegressor(
            objective="reg:squarederror",
            random_state=RANDOM_STATE,
            n_jobs=-1,
            **xgb_params,
        )),
    ])


def fit_xgboost_margin_model(train_df: pd.DataFrame, n_iter: int = DEFAULT_N_ITER):
    """
    XGBoost counterpart to model.fit_margin_model. Returns (fitted_pipeline,
    residual_std) with the exact same shape/meaning as the Ridge version, so
    predict_margin()/margin_to_home_win_prob()/margin_to_model_spread_home()
    all work unchanged on either model's output -- model_comparison.py relies
    on that to grade both through the same code path.
    """
    X, y = train_df[FEATURE_COLS], train_df["margin"]
    n = len(train_df)

    if n < 30:
        # Too little data for a meaningful hyperparameter search -- fixed,
        # deliberately shallow/conservative defaults (few, shallow trees)
        # rather than a search that would just overfit whichever handful of
        # games happen to be available early in a season.
        pipe = build_xgb_pipeline(n_estimators=100, max_depth=2, learning_rate=0.1,
                                   subsample=1.0, colsample_bytree=1.0,
                                   min_child_weight=5, reg_lambda=5.0)
        pipe.fit(X, y)
        resid = y - pipe.predict(X)
        residual_std = float(np.std(resid)) if len(resid) else 14.0
        return pipe, residual_std

    n_folds = 5 if n >= 50 else max(2, min(5, n // 10))
    search = RandomizedSearchCV(
        build_xgb_pipeline(), XGB_PARAM_GRID,
        n_iter=min(n_iter, 30), cv=n_folds,
        scoring="neg_mean_absolute_error",
        random_state=RANDOM_STATE, n_jobs=-1,
    )
    search.fit(X, y)
    best_pipe = search.best_estimator_

    # Out-of-fold residuals from the BEST params, same reasoning as
    # fit_margin_model: in-sample residuals understate real error.
    oof_pred = cross_val_predict(best_pipe, X, y, cv=n_folds)
    resid = y - oof_pred
    best_pipe.fit(X, y)  # final model refit on all training data
    residual_std = float(np.std(resid)) if len(resid) else 14.0
    return best_pipe, residual_std
