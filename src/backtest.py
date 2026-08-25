"""
Phase 3, Section 6 of the attack plan: a walk-forward backtest of the margin
model against the market spread.

The one non-negotiable rule from the plan: never fit or tune using data from
after the game being predicted. This script enforces that literally -- for
every test week, the model is retrained from scratch using only games whose
start_date is strictly before that week's earliest kickoff, and it forgets
that fit before moving to the next week. This is expanding-window walk-
forward, so early seasons are pure training data and the harness only starts
grading once there's a reasonable amount of history behind it.

Metrics computed, per the plan:
  - ATS win rate (need ~52.4% at -110 to break even)
  - CLV (closing line value) -- did the number you'd have bet move in your
    favor by closing? Positive = yes, computed per the side actually bet.
  - Calibration (Brier score) -- do stated win probabilities match reality?
  - ROI at flat 1-unit stake, -110 odds
  - All of the above sliced overall AND filtered to games involving a 2026
    Mountain West team, since the whole point is MW-specific edges.

Usage:
    source .venv/bin/activate
    python src/backtest.py
"""
import duckdb
import numpy as np
import pandas as pd

from config import DB_PATH, CLEAN_DIR
import model
from teams import MW_TEAMS_2026

EDGE_THRESHOLD = 2.0     # points -- matches the Excel tracker's Settings default
MIN_TRAIN_GAMES = 100    # roughly two synthetic/actual seasons before grading starts


def _test_weeks(con):
    return con.execute("""
        SELECT season, week, MIN(start_date) AS week_start
        FROM games
        WHERE completed = TRUE
        GROUP BY season, week
        ORDER BY week_start
    """).fetchdf()


def run_backtest(con, edge_threshold=EDGE_THRESHOLD, min_train_games=MIN_TRAIN_GAMES) -> pd.DataFrame:
    weeks = _test_weeks(con)
    full_pool = model.load_training_frame(con)  # every completed game w/ a market line, for slicing test rows out of
    full_pool = full_pool.set_index("game_id")

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

        pipe, residual_std = model.fit_margin_model(train_df)
        pred_margin = model.predict_margin(pipe, test_df)
        model_spread_home = -pred_margin
        home_win_prob = model.margin_to_home_win_prob(pred_margin, residual_std)

        for i, (game_id, row) in enumerate(test_df.iterrows()):
            market_close = row["market_spread_home"]
            market_open = row["market_spread_home_open"]
            edge = market_close - model_spread_home[i]
            lean = "Home" if edge > 0 else ("Away" if edge < 0 else "Pick'em")

            actual_margin = row["margin"]
            cover_value = actual_margin + market_close
            if cover_value > 0:
                home_covers = True
            elif cover_value < 0:
                home_covers = False
            else:
                home_covers = None  # push

            is_bet = abs(edge) >= edge_threshold and lean != "Pick'em"
            bet_result = None
            clv = None
            if is_bet:
                if home_covers is None:
                    bet_result = "Push"
                elif (lean == "Home") == home_covers:
                    bet_result = "Win"
                else:
                    bet_result = "Loss"
                # CLV in the units of the side actually bet -- positive means
                # you'd have gotten a worse number for that side by closing.
                clv = (market_open - market_close) if lean == "Home" else (market_close - market_open)

            actual_home_win = 1.0 if actual_margin > 0 else (0.0 if actual_margin < 0 else 0.5)

            results.append({
                "game_id": game_id, "season": row["season"], "week": row["week"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "is_mw_game": row["home_team"] in MW_TEAMS_2026 or row["away_team"] in MW_TEAMS_2026,
                "model_spread_home": model_spread_home[i], "market_spread_home": market_close,
                "edge": edge, "lean": lean, "is_bet": is_bet, "bet_result": bet_result, "clv": clv,
                "home_win_prob": home_win_prob[i], "actual_home_win": actual_home_win,
                "actual_margin": actual_margin,
            })

    return pd.DataFrame(results)


def summarize(df: pd.DataFrame, label: str) -> dict:
    if df.empty:
        return {"slice": label, "n_games": 0}
    bets = df[df["is_bet"] & df["bet_result"].isin(["Win", "Loss"])]
    wins = (bets["bet_result"] == "Win").sum()
    losses = (bets["bet_result"] == "Loss").sum()
    pushes = (df["is_bet"] & (df["bet_result"] == "Push")).sum()
    n_bets = wins + losses
    ats_win_rate = wins / n_bets if n_bets else float("nan")
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n_bets if n_bets else float("nan")
    brier = float(np.mean((df["home_win_prob"] - df["actual_home_win"]) ** 2))
    mean_clv = bets["clv"].mean() if n_bets else float("nan")
    return {
        "slice": label, "n_games": len(df), "n_bets": int(n_bets),
        "wins": int(wins), "losses": int(losses), "pushes": int(pushes),
        "ats_win_rate": round(ats_win_rate, 4) if n_bets else None,
        "roi_flat_stake": round(roi, 4) if n_bets else None,
        "brier_score": round(brier, 4),
        "mean_clv_pts": round(mean_clv, 3) if n_bets else None,
    }


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_backtest(con)
    con.close()

    if df.empty:
        print(
            "backtest: not enough graded games yet. This needs completed games with "
            f"market lines, and at least {MIN_TRAIN_GAMES} of them before the walk-forward "
            "window starts testing -- normal early in the season or with limited history pulled."
        )
        return

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CLEAN_DIR / "backtest_results.csv"
    df.to_csv(out_path, index=False)
    print(f"Per-game results written to {out_path} ({len(df)} rows)\n")

    overall = summarize(df, "Overall (all FBS)")
    mw = summarize(df[df["is_mw_game"]], "Mountain West-involved")
    for s in (overall, mw):
        print(f"--- {s['slice']} ---")
        for k, v in s.items():
            if k != "slice":
                print(f"  {k}: {v}")
        print()


if __name__ == "__main__":
    main()