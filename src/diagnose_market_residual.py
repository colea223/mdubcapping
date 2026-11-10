"""
Can a model beat the closing line by predicting the MARKET'S MISS (actual margin minus the Vegas margin) instead of the
margin itself?  READ-ONLY on the database.

Why: Ridge/XGBoost trained to predict the margin from scratch land at ~50% ATS over 2016-2025 (8,000+ games) and are
~1.7 points worse than Vegas on average miss. A model that only predicts small corrections AROUND the line is a much
more realistic way to find an edge, and ten seasons of history means the answer is statistically meaningful.

Setup (walk-forward by SEASON, nothing from the test season is used to train):
    target r = actual home margin - market home margin  (market home margin = -market_spread_home)
    features = the same game-level features model.py uses (auto-detected below; printed so you can see them)
    models   = heavily regularized Ridge, and a small shallow gradient-boosting model
    pick     = Home if predicted r > 0 else Away; only 'confident' picks (|pred r| >= t) are also scored
Reports ATS% for test seasons 2018-2025 overall, by confidence band, and by season. Break-even at -110 is 52.4%.

Usage:
    python src/diagnose_market_residual.py
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DB_PATH  # noqa: E402
import model  # noqa: E402

EXCLUDE = {"game_id", "season", "week", "margin", "market_spread_home", "home_points", "away_points", "total",
           "market_total", "start_date", "home_win", "season_type", "completed"}
FIRST_TEST_SEASON = 2018
BANDS = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 99)]


def pick_features(df):
    cands = None
    for name in ("FEATURES", "FEATURE_COLUMNS", "FEATURE_COLS", "NUMERIC_FEATURES", "MARGIN_FEATURES"):
        cands = getattr(model, name, None)
        if cands:
            break
    if cands:
        cols = [c for c in cands if c in df.columns]
        src = f"model.{name}"
    else:
        cols = [c for c in df.columns if c not in EXCLUDE and pd.api.types.is_numeric_dtype(df[c])]
        src = "all numeric columns (model.py feature list not found)"
    return cols, src


def evaluate(pred_r, df, label):
    r_true = df["margin"] + df["market_spread_home"]
    ok = r_true != 0
    p, r = pred_r[ok], r_true[ok]
    win = np.sign(p) == np.sign(r)
    res = pd.DataFrame({"win": win.values, "ae": np.abs(p).values, "season": df.loc[ok.values, "season"].values})
    print(f"\n{label}: {len(res)} games, ATS {100 * res.win.mean():.1f}%")
    print("  by |predicted miss| band:", {f"{a}-{b}": (int(((res.ae >= a) & (res.ae < b)).sum()),
          round(100 * res[(res.ae >= a) & (res.ae < b)].win.mean(), 1)) for a, b in BANDS})
    print("  by season:", res.groupby("season").win.mean().mul(100).round(1).to_dict())
    for t in (1, 2, 3):
        x = res[res.ae >= t]
        print(f"  only |pred| >= {t}: {len(x)} bets, {100 * x.win.mean():.1f}%")


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    df = model.load_training_frame(con).dropna(subset=["market_spread_home", "margin"]).copy()
    cols, src = pick_features(df)
    print(f"{len(df)} games with a closing line, seasons {df.season.min()}-{df.season.max()}.")
    print(f"{len(cols)} features from {src}:\n  {cols}")
    df["r"] = df["margin"] + df["market_spread_home"]
    ridge = lambda: make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=3000.0))
    gbm = lambda: HistGradientBoostingRegressor(max_depth=3, learning_rate=0.03, max_iter=150,
                                                l2_regularization=10.0, min_samples_leaf=80, random_state=0)
    preds = {"Ridge on market miss": [], "GBM on market miss": [], "Ridge, shrunk 50%": []}
    idx = []
    for s in sorted(df.season.unique()):
        if s < FIRST_TEST_SEASON or s == 2026:       # 2026 is still in progress
            continue
        tr, te = df[df.season < s], df[df.season == s]
        m1 = ridge().fit(tr[cols], tr["r"])
        m2 = gbm().fit(tr[cols], tr["r"])
        p1, p2 = m1.predict(te[cols]), m2.predict(te[cols])
        preds["Ridge on market miss"].append(pd.Series(p1, index=te.index))
        preds["GBM on market miss"].append(pd.Series(p2, index=te.index))
        preds["Ridge, shrunk 50%"].append(pd.Series(0.5 * p1, index=te.index))
        idx.append(te.index)
        print(f"  trained on seasons < {s}, tested {s} ({len(te)} games)", flush=True)
    test = df.loc[np.concatenate([i.values for i in idx])]
    for label, parts in preds.items():
        evaluate(pd.concat(parts).loc[test.index], test, label)
    print("\nBreak-even at -110 is 52.4%. A real edge should show up in MOST seasons and rise with confidence;")
    print("a single good season or a flat band table means it is noise.")


if __name__ == "__main__":
    main()
