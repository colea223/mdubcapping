"""
Phase 3, Section 6 of the attack plan: a walk-forward backtest of the model
against the market -- spread, total (over/under), AND moneyline, each graded
with real units at real odds (moneyline uses the actual home/away American
odds from that game, not an assumed price; spread and total use -110, the
standard price absent a book-specific number in CFBD's lines data).

The one non-negotiable rule from the plan: never fit or tune using data from
after the game being predicted. This script enforces that literally -- for
every test week, the model (and the totals baseline) is rebuilt from scratch
using only games whose start_date is strictly before that week's earliest
kickoff, and it forgets that fit before moving to the next week. This is
expanding-window walk-forward, so early seasons are pure training data and
the harness only starts grading once there's a reasonable amount of history
behind it.

Metrics computed, per the plan, for EACH of spread/total/moneyline:
  - Win rate (spread/total need ~52.4% at -110 to break even; moneyline's
    break-even rate depends on the odds actually taken, so ROI is the number
    that actually matters there)
  - CLV (closing line value) for spread/total -- did the number move in your
    favor by closing? Positive = yes. Skipped for moneyline: CFBD's lines
    table only stores one moneyline snapshot per game, not an opening price,
    so there's nothing to compare against.
  - Calibration (Brier score) -- do stated win probabilities match reality?
    (shared across bet types since it's about the model's win-prob estimate)
  - ROI at flat 1-unit stake -- -110 for spread/total, real odds for moneyline
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
import totals_model
import time
from odds import no_vig_prob, payout_profit
from teams import MW_TEAMS_2026

EDGE_THRESHOLD = 2.0        # points -- matches the Excel tracker's Settings default (spread AND total)
ML_EDGE_THRESHOLD = 0.05    # model win prob vs. no-vig market prob, in probability points
MIN_TRAIN_GAMES = 100    # roughly two synthetic/actual seasons before grading starts


def _test_weeks(con):
    return con.execute("""
        SELECT season, week, MIN(start_date) AS week_start
        FROM games
        WHERE completed = TRUE
        GROUP BY season, week
        ORDER BY week_start
    """).fetchdf()


def run_backtest(con, edge_threshold=EDGE_THRESHOLD, ml_edge_threshold=ML_EDGE_THRESHOLD,
                  min_train_games=MIN_TRAIN_GAMES) -> pd.DataFrame:
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

        # Same walk-forward discipline for the totals model -- rebuilt per
        # test week from only games strictly before it. See
        # src/totals_model.py for why this SP+/PPA-based regression replaced
        # the old raw-scoring-average baseline (model.totals_baseline()).
        totals_train = totals_model.load_totals_training_frame(con, before_date=wk.week_start)
        total_pipe, _ = totals_model.fit_total_model(totals_train)
        wk_totals_features = totals_model.load_upcoming_totals_frame(con, wk.season, wk.week)
        model_total_map = {}
        if not wk_totals_features.empty:
            total_preds = totals_model.predict_total(total_pipe, wk_totals_features)
            model_total_map = dict(zip(wk_totals_features["game_id"].astype(int), total_preds))
        model_total = [model_total_map.get(int(game_id)) for game_id in test_df.index]

        for i, (game_id, row) in enumerate(test_df.iterrows()):
            # ---------------------------------------------------- spread
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

            # ---------------------------------------------------- total (over/under)
            market_total_close = row["market_total"]
            market_total_open = row["market_total_open"]
            actual_total = row["home_points"] + row["away_points"]
            total_edge, total_lean, is_total_bet, total_bet_result, total_clv = (None,) * 5
            if pd.notna(market_total_close) and model_total[i] is not None:
                total_edge = model_total[i] - market_total_close
                total_lean = "Over" if total_edge > 0 else ("Under" if total_edge < 0 else "Pick'em")
                is_total_bet = abs(total_edge) >= edge_threshold and total_lean != "Pick'em"
                if is_total_bet:
                    if actual_total == market_total_close:
                        total_bet_result = "Push"
                    elif (total_lean == "Over") == (actual_total > market_total_close):
                        total_bet_result = "Win"
                    else:
                        total_bet_result = "Loss"
                    if pd.notna(market_total_open):
                        # Over wants the total to have been LOW when bet and to
                        # rise by closing (market agreeing more games go over);
                        # mirror image for Under.
                        total_clv = ((market_total_close - market_total_open) if total_lean == "Over"
                                     else (market_total_open - market_total_close))

            # ---------------------------------------------------- moneyline
            home_ml, away_ml = row["market_home_ml"], row["market_away_ml"]
            ml_lean, is_ml_bet, ml_bet_result, ml_profit, ml_edge = (None,) * 5
            if pd.notna(home_ml) and pd.notna(away_ml):
                market_home_prob = no_vig_prob(home_ml, away_ml)
                ml_edge = home_win_prob[i] - market_home_prob
                ml_lean = "Home" if ml_edge > 0 else "Away"
                is_ml_bet = abs(ml_edge) >= ml_edge_threshold
                if is_ml_bet:
                    home_won = actual_margin > 0  # CFB has no ties -- no push case here
                    won = home_won if ml_lean == "Home" else (not home_won)
                    ml_bet_result = "Win" if won else "Loss"
                    ml_profit = payout_profit(1.0, home_ml if ml_lean == "Home" else away_ml, won)

            results.append({
                "game_id": game_id, "season": row["season"], "week": row["week"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "is_mw_game": row["home_team"] in MW_TEAMS_2026 or row["away_team"] in MW_TEAMS_2026,
                "model_spread_home": model_spread_home[i], "market_spread_home": market_close,
                "edge": edge, "lean": lean, "is_bet": is_bet, "bet_result": bet_result, "clv": clv,
                "home_win_prob": home_win_prob[i], "actual_home_win": actual_home_win,
                "actual_margin": actual_margin,
                "model_total": model_total[i], "market_total": market_total_close,
                "total_edge": total_edge, "total_lean": total_lean, "is_total_bet": is_total_bet,
                "total_bet_result": total_bet_result, "total_clv": total_clv, "actual_total": actual_total,
                "market_home_ml": home_ml, "market_away_ml": away_ml,
                "ml_edge": ml_edge, "ml_lean": ml_lean, "is_ml_bet": is_ml_bet,
                "ml_bet_result": ml_bet_result, "ml_profit": ml_profit,
            })

    return pd.DataFrame(results)


def summarize(df: pd.DataFrame, label: str) -> dict:
    """Spread (ATS) summary -- flat -110 odds, the standard spread price."""
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


def summarize_totals(df: pd.DataFrame, label: str) -> dict:
    """Over/under summary -- flat -110 odds, same as spread."""
    if df.empty:
        return {"slice": label, "n_games": 0}
    graded = df.dropna(subset=["model_total", "actual_total"])
    bets = df[(df["is_total_bet"] == True) & (df["total_bet_result"].isin(["Win", "Loss"]))]
    wins = (bets["total_bet_result"] == "Win").sum()
    losses = (bets["total_bet_result"] == "Loss").sum()
    pushes = ((df["is_total_bet"] == True) & (df["total_bet_result"] == "Push")).sum()
    n_bets = wins + losses
    win_rate = wins / n_bets if n_bets else float("nan")
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n_bets if n_bets else float("nan")
    mae = float(np.mean(np.abs(graded["model_total"] - graded["actual_total"]))) if not graded.empty else None
    mean_clv = bets["total_clv"].mean() if n_bets else float("nan")
    return {
        "slice": label, "n_games": len(df), "n_bets": int(n_bets),
        "wins": int(wins), "losses": int(losses), "pushes": int(pushes),
        "win_rate": round(win_rate, 4) if n_bets else None,
        "roi_flat_stake": round(roi, 4) if n_bets else None,
        "mean_abs_error_pts": round(mae, 2) if mae is not None else None,
        "mean_clv_pts": round(mean_clv, 3) if n_bets and pd.notna(mean_clv) else None,
    }


def summarize_moneyline(df: pd.DataFrame, label: str) -> dict:
    """Moneyline summary -- REAL odds per bet, not a flat assumed price."""
    if df.empty:
        return {"slice": label, "n_games": 0}
    bets = df[(df["is_ml_bet"] == True) & (df["ml_bet_result"].isin(["Win", "Loss"]))]
    wins = (bets["ml_bet_result"] == "Win").sum()
    losses = (bets["ml_bet_result"] == "Loss").sum()
    n_bets = wins + losses
    win_rate = wins / n_bets if n_bets else float("nan")
    profit = bets["ml_profit"].sum() if n_bets else 0.0
    roi = profit / n_bets if n_bets else float("nan")
    brier = float(np.mean((df["home_win_prob"] - df["actual_home_win"]) ** 2))
    return {
        "slice": label, "n_games": len(df), "n_bets": int(n_bets),
        "wins": int(wins), "losses": int(losses),
        "win_rate": round(win_rate, 4) if n_bets else None,
        "units_won": round(float(profit), 2) if n_bets else None,
        "roi_flat_stake": round(roi, 4) if n_bets else None,
        "brier_score": round(brier, 4),
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

    slices = [("Overall (all FBS)", df), ("Mountain West-involved", df[df["is_mw_game"]])]
    for bet_type, fn in [("SPREAD", summarize), ("TOTAL", summarize_totals), ("MONEYLINE", summarize_moneyline)]:
        print(f"=== {bet_type} ===")
        for label, sl in slices:
            s = fn(sl, label)
            print(f"--- {s['slice']} ---")
            for k, v in s.items():
                if k != "slice":
                    print(f"  {k}: {v}")
            print()


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
