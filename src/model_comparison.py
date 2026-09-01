"""
Walk-forward comparison of the two margin-of-victory models -- Ridge (model.py,
the live/production model) and XGBoost (xgboost_model.py, a candidate) --
against each other AND against Vegas's closing spread. Spread only (not
totals/moneyline): XGBoost here is a drop-in alternative to model.py's margin
model specifically, and totals_model.py is a separate, already-validated
piece of machinery this comparison doesn't touch.

Same walk-forward discipline as backtest.py: for every test week, BOTH models
are refit from scratch using only games whose start_date is strictly before
that week's earliest kickoff, then graded on that week only, then forgotten.
Neither model ever sees a future result while being tuned or fit.

This is deliberately a standalone script, same category backtest.py already
established for itself in run_pipeline.py's own comments ("an evaluation
report, not a data step") -- it is NOT wired into run_pipeline.py or the
GitHub Actions workflows. Two reasons: GitHub Actions' 30-minute timeout, and
because refitting an XGBoost hyperparameter search for every historical week
is meaningfully more compute than the Ridge-only backtest already does. Run
it by hand whenever you want a fresh read on how the two compare.

Usage:
    source .venv/bin/activate
    python src/model_comparison.py
    python excel/update_model_comparison_tab.py   # writes the result into the tracker
"""
import duckdb
import numpy as np
import pandas as pd

from config import DB_PATH, CLEAN_DIR
import model
import xgboost_model
from teams import MW_TEAMS_2026
from backtest import _test_weeks, EDGE_THRESHOLD, MIN_TRAIN_GAMES


def _grade_side(model_spread_home, market_close, actual_margin, edge_threshold):
    """Shared spread-grading logic (same rules as backtest.py's spread block),
    factored out here so Ridge and XGBoost are graded through identical code."""
    edge = market_close - model_spread_home
    lean = "Home" if edge > 0 else ("Away" if edge < 0 else "Pick'em")
    cover_value = actual_margin + market_close
    if cover_value > 0:
        home_covers = True
    elif cover_value < 0:
        home_covers = False
    else:
        home_covers = None  # push

    is_bet = abs(edge) >= edge_threshold and lean != "Pick'em"
    result = None
    if is_bet:
        if home_covers is None:
            result = "Push"
        elif (lean == "Home") == home_covers:
            result = "Win"
        else:
            result = "Loss"
    return edge, lean, is_bet, result


def run_comparison(con, edge_threshold=EDGE_THRESHOLD, min_train_games=MIN_TRAIN_GAMES) -> pd.DataFrame:
    weeks = _test_weeks(con)
    full_pool = model.load_training_frame(con).set_index("game_id")

    results = []
    for wk in weeks.itertuples():
        train_df = model.load_training_frame(con, before_date=wk.week_start)
        train_df = train_df.dropna(subset=["market_spread_home"])
        if len(train_df) < min_train_games:
            continue

        test_mask = (full_pool["season"] == wk.season) & (full_pool["week"] == wk.week)
        test_df = full_pool[test_mask].dropna(subset=["market_spread_home"])
        if test_df.empty:
            continue

        ridge_pipe, ridge_resid = model.fit_margin_model(train_df)
        ridge_pred_margin = model.predict_margin(ridge_pipe, test_df)
        ridge_spread_home = -ridge_pred_margin

        xgb_pipe, xgb_resid = xgboost_model.fit_xgboost_margin_model(train_df)
        xgb_pred_margin = model.predict_margin(xgb_pipe, test_df)
        xgb_spread_home = -xgb_pred_margin

        for i, (game_id, row) in enumerate(test_df.iterrows()):
            market_close = row["market_spread_home"]
            actual_margin = row["margin"]

            r_edge, r_lean, r_is_bet, r_result = _grade_side(
                ridge_spread_home[i], market_close, actual_margin, edge_threshold)
            x_edge, x_lean, x_is_bet, x_result = _grade_side(
                xgb_spread_home[i], market_close, actual_margin, edge_threshold)

            results.append({
                "game_id": game_id, "season": row["season"], "week": row["week"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "is_mw_game": row["home_team"] in MW_TEAMS_2026 or row["away_team"] in MW_TEAMS_2026,
                "market_spread_home": market_close, "actual_margin": actual_margin,
                "ridge_spread_home": ridge_spread_home[i], "ridge_edge": r_edge,
                "ridge_lean": r_lean, "ridge_is_bet": r_is_bet, "ridge_result": r_result,
                "xgb_spread_home": xgb_spread_home[i], "xgb_edge": x_edge,
                "xgb_lean": x_lean, "xgb_is_bet": x_is_bet, "xgb_result": x_result,
                "models_agree": (r_lean == x_lean) if (r_lean != "Pick'em" and x_lean != "Pick'em") else None,
            })

    return pd.DataFrame(results)


def summarize(df: pd.DataFrame, prefix: str, label: str) -> dict:
    """prefix is 'ridge' or 'xgb' -- picks which model's columns to grade."""
    if df.empty:
        return {"slice": label, "model": prefix, "n_games": 0}
    bets = df[df[f"{prefix}_is_bet"] & df[f"{prefix}_result"].isin(["Win", "Loss"])]
    wins = (bets[f"{prefix}_result"] == "Win").sum()
    losses = (bets[f"{prefix}_result"] == "Loss").sum()
    pushes = (df[f"{prefix}_is_bet"] & (df[f"{prefix}_result"] == "Push")).sum()
    n_bets = wins + losses
    win_rate = wins / n_bets if n_bets else float("nan")
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n_bets if n_bets else float("nan")
    mae = float(np.mean(np.abs(df[f"{prefix}_spread_home"] + df["market_spread_home"])))  # vs. Vegas, informational
    return {
        "slice": label, "model": prefix, "n_games": len(df), "n_bets": int(n_bets),
        "wins": int(wins), "losses": int(losses), "pushes": int(pushes),
        "ats_win_rate": round(win_rate, 4) if n_bets else None,
        "roi_flat_stake": round(roi, 4) if n_bets else None,
    }


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_comparison(con)
    con.close()

    if df.empty:
        print(
            "model_comparison: not enough graded games yet. Same requirement as backtest.py -- "
            f"needs completed games with market lines and at least {MIN_TRAIN_GAMES} of them "
            "before the walk-forward window starts testing."
        )
        return

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CLEAN_DIR / "model_comparison_results.csv"
    df.to_csv(out_path, index=False)
    print(f"Per-game results written to {out_path} ({len(df)} rows)\n")

    agree_rate = df["models_agree"].dropna().mean() if df["models_agree"].notna().any() else None
    if agree_rate is not None:
        print(f"Ridge and XGBoost agree on which side to lean in {agree_rate:.1%} of graded games\n")

    slices = [("Overall (all FBS)", df), ("Mountain West-involved", df[df["is_mw_game"]])]
    for label, sl in slices:
        print(f"=== {label} ===")
        for prefix, name in [("ridge", "RIDGE (live model)"), ("xgb", "XGBOOST (candidate)")]:
            s = summarize(sl, prefix, label)
            print(f"--- {name} ---")
            for k, v in s.items():
                if k not in ("slice", "model"):
                    print(f"  {k}: {v}")
        print()


if __name__ == "__main__":
    main()
