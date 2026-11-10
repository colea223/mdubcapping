"""
Does betting only when Dub Gamma AND a market-miss model agree beat betting Gamma alone?  READ-ONLY.

Market-miss model: gradient-boosted trees trained on 2016-2025 games to predict (actual home margin - market home margin)
from the 22 game features (same setup as diagnose_market_residual.py). For each 2026 game it says "the home side will cover
the line" (predicted miss > 0) or "the away side will". Gamma's pick comes from data/clean/model_comparison_results.csv
(walk-forward, weeks 2+; week 1 is Moore's seed and is excluded). Run `git pull` first so that file is current.

Reports ATS% (pushes excluded) for: all Gamma picks, picks where both agree, picks where they disagree, and the agreement
set restricted to the market-miss model's more confident calls. Break-even at -110 is 52.4%. The sample is one season,
so treat any difference under ~3 points as noise.

Usage:
    python src/diagnose_agreement_filter.py
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DB_PATH, CLEAN_DIR  # noqa: E402
import model  # noqa: E402

SEASON = 2026


def summary(label, d):
    d = d[d["res"] != "P"]
    if d.empty:
        print(f"  {label:<46} no games")
        return
    w = int((d["res"] == "W").sum())
    n = len(d)
    pct = 100 * w / n
    profit = w * 90.909 - (n - w) * 100
    print(f"  {label:<46} {w:>3}-{n - w:<3} {pct:5.1f}%   ${profit:>7,.0f} per $100/game")


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    df = model.load_training_frame(con).dropna(subset=["market_spread_home", "margin"]).copy()
    cols = [c for c in model.FEATURE_COLS if c in df.columns]
    df["r"] = df["margin"] + df["market_spread_home"]
    tr, te = df[df.season < SEASON], df[df.season == SEASON]
    gbm = HistGradientBoostingRegressor(max_depth=3, learning_rate=0.03, max_iter=150, l2_regularization=10.0,
                                        min_samples_leaf=80, random_state=0).fit(tr[cols], tr["r"])
    te = te.assign(gbm_r=gbm.predict(te[cols]))[["game_id", "gbm_r"]]
    print(f"Trained on {len(tr)} games (2016-2025); scoring {len(te)} games from {SEASON}.")

    cmp_ = pd.read_csv(CLEAN_DIR / "model_comparison_results.csv")
    cmp_ = cmp_[(cmp_.season == SEASON) & (cmp_.week >= 2) & cmp_.gamma_spread_home.notna()
                & cmp_.market_spread_home.notna()].merge(te, on="game_id", how="inner")
    print(f"{len(cmp_)} {SEASON} games (weeks {int(cmp_.week.min())}-{int(cmp_.week.max())}) have both Gamma and the market-miss model.\n")

    edge = cmp_["market_spread_home"] - cmp_["gamma_spread_home"]
    gamma_home = np.where(edge.abs() < 1, cmp_["market_spread_home"] < 0, edge > 0)    # same <1pt favorite rule as the site
    cover = cmp_["actual_margin"] + cmp_["market_spread_home"]
    cmp_["res"] = np.where(cover == 0, "P", np.where((cover > 0) == gamma_home, "W", "L"))
    gbm_home = cmp_["gbm_r"] > 0
    agree = gbm_home == gamma_home
    cmp_["agree"] = agree
    cmp_["conf"] = cmp_["gbm_r"].abs()

    print("Gamma picks (every game, <1pt = market favorite):")
    summary("all Gamma picks", cmp_)
    summary("  market-miss model AGREES", cmp_[agree])
    summary("  market-miss model DISAGREES", cmp_[~agree])
    print("\nGamma real-edge picks only (|edge| >= 2):")
    big = edge.abs() >= 2
    summary("all", cmp_[big])
    summary("  agree", cmp_[big & agree])
    summary("  disagree", cmp_[big & ~agree])
    print("\nGamma edge 1-4 points (excludes the 4+ point gaps that have been losing):")
    mid = (edge.abs() >= 1) & (edge.abs() < 4)
    summary("all", cmp_[mid])
    summary("  agree", cmp_[mid & agree])
    summary("  disagree", cmp_[mid & ~agree])
    print("\nAgreement, by how confident the market-miss model is:")
    for t in (0.5, 1.0, 1.5, 2.0):
        summary(f"  agree and |market-miss| >= {t}", cmp_[agree & (cmp_.conf >= t)])
    print("\nBy week (agree only):")
    for wk, g in cmp_[agree].groupby("week"):
        summary(f"  week {int(wk)}", g)
    print("\nOne season is a small sample: a 3-point gap on ~200 games is roughly 6 games, i.e. luck-sized.")


if __name__ == "__main__":
    main()
