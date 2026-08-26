"""
Sensitivity sweep for the TOTAL bet edge threshold, run against real
backtest data.

backtest.py currently uses ONE EDGE_THRESHOLD = 2.0 for BOTH spread and total
bets. But a game's total is the combined score of both teams, which carries
more variance than a single-game spread margin -- so the same raw point-gap
bar that works for spread might be too loose for totals, letting through
marginal bets that lose money on net even when spread and moneyline are
profitable at that same threshold.

Rather than guess at a better number, this script runs the (expensive,
walk-forward) backtest ONCE, then re-grades TOTAL bets at several candidate
thresholds using the model's raw total_edge for each game. This works because
edge computation doesn't depend on the threshold at all -- run_backtest()
computes model_total/total_edge for EVERY graded game regardless of
edge_threshold; the threshold only decides which ones clear the bar to count
as a placed bet. So one backtest run answers the whole sweep, instead of
re-fitting the walk-forward model once per candidate threshold.

Note: CLV isn't included in this sweep's output -- run_backtest()'s returned
per-game frame doesn't retain market_total_open (only the closing total), so
it can't be recomputed for thresholds other than the one the frame was
originally graded at. Win rate and ROI are what actually decide the right
threshold anyway (break-even at -110 is 52.4% win rate, but ROI is the number
that matters since fewer-but-more-accurate bets can beat more-but-weaker
ones).

Usage:
    source .venv/bin/activate
    python src/sweep_total_threshold.py
"""
import duckdb
import pandas as pd

from config import DB_PATH
from backtest import run_backtest, EDGE_THRESHOLD, MIN_TRAIN_GAMES

CANDIDATE_THRESHOLDS = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]


def summarize_totals_at(df: pd.DataFrame, threshold: float, label: str) -> dict:
    """
    Re-grades TOTAL bets at `threshold` using the raw total_edge/actual_total/
    market_total columns already in df -- NOT df's own total_bet_result/
    is_total_bet, which were computed at whatever edge_threshold run_backtest()
    was originally called with (see module docstring).
    """
    graded = df.dropna(subset=["total_edge", "actual_total", "market_total"])
    if graded.empty:
        return {"slice": label, "threshold": threshold, "n_games": len(df), "n_bets": 0}

    bettable = graded[graded["total_edge"].abs() >= threshold]
    if bettable.empty:
        return {"slice": label, "threshold": threshold, "n_games": len(df), "n_bets": 0}

    lean_over = bettable["total_edge"] > 0
    push = bettable["actual_total"] == bettable["market_total"]
    actual_over = bettable["actual_total"] > bettable["market_total"]
    win = (~push) & (lean_over == actual_over)
    loss = (~push) & (lean_over != actual_over)

    wins, losses, pushes = int(win.sum()), int(loss.sum()), int(push.sum())
    n_bets = wins + losses
    win_rate = wins / n_bets if n_bets else None
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n_bets if n_bets else None

    return {
        "slice": label, "threshold": threshold, "n_games": len(df),
        "n_bets": n_bets, "wins": wins, "losses": losses, "pushes": pushes,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "roi_flat_stake": round(roi, 4) if roi is not None else None,
    }


def main():
    con = duckdb.connect(str(DB_PATH))
    # edge_threshold/ml_edge_threshold passed here don't affect this sweep's
    # totals numbers at all -- see module docstring -- so just use the
    # project defaults.
    df = run_backtest(con, min_train_games=MIN_TRAIN_GAMES)
    con.close()

    if df.empty:
        print(
            "sweep: not enough graded games yet. This needs completed games with "
            f"market lines, and at least {MIN_TRAIN_GAMES} of them before the walk-forward "
            "window starts testing -- run the full pipeline first."
        )
        return

    slices = [("Overall (all FBS)", df), ("Mountain West-involved", df[df["is_mw_game"]])]

    header = f"{'Threshold':>9} | {'Slice':<22} | {'N Bets':>7} | {'Win Rate':>9} | {'ROI (flat 1u)':>14}"
    print(header)
    print("-" * len(header))
    for threshold in CANDIDATE_THRESHOLDS:
        for label, sl in slices:
            s = summarize_totals_at(sl, threshold, label)
            wr = f"{s['win_rate'] * 100:.1f}%" if s.get("win_rate") is not None else "--"
            roi = f"{s['roi_flat_stake'] * 100:+.2f}%" if s.get("roi_flat_stake") is not None else "--"
            marker = "  <- current" if threshold == EDGE_THRESHOLD else ""
            print(f"{threshold:>9.1f} | {label:<22} | {s.get('n_bets', 0):>7} | {wr:>9} | {roi:>14}{marker}")
        print()

    print(f"Reference: backtest.py's current shared EDGE_THRESHOLD is {EDGE_THRESHOLD} (used for spread too --")
    print("this sweep only touches totals; spread's own threshold is untouched either way).")
    print("Break-even at -110 is 52.4% win rate, but ROI is what actually decides it -- watch how n_bets")
    print("shrinks as the threshold rises (fewer, more selective bets) alongside whether ROI improves.")


if __name__ == "__main__":
    main()
