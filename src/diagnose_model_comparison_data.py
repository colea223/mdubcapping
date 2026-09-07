"""
One-off, READ-ONLY diagnostic for "model_comparison: not enough graded games
yet" -- narrows down exactly which stage is empty (no completed games, no
game_features, no market lines joined in, or every week just genuinely
hasn't cleared the MIN_TRAIN_GAMES walk-forward warm-up yet) without doing
any of the slow Ridge/XGBoost refitting model_comparison.py itself does.
Prints counts only; makes no changes to the database.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python src/diagnose_model_comparison_data.py
"""
import duckdb

from config import DB_PATH
import model
from backtest import _test_weeks, MIN_TRAIN_GAMES


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)

    total_games = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    completed_games = con.execute("SELECT COUNT(*) FROM games WHERE completed = TRUE").fetchone()[0]
    game_features_rows = con.execute("SELECT COUNT(*) FROM game_features").fetchone()[0]
    lines_rows = con.execute("SELECT COUNT(*) FROM lines").fetchone()[0]

    print(f"total games in DB:            {total_games}")
    print(f"completed games:              {completed_games}")
    print(f"game_features rows:           {game_features_rows}")
    print(f"lines rows (raw odds pulls):  {lines_rows}")

    weeks = _test_weeks(con)
    print(f"\ntest week candidates (completed, grouped by season/week): {len(weeks)}")

    full_pool = model.load_training_frame(con).set_index("game_id")
    n_with_market = int(full_pool["market_spread_home"].notna().sum())
    print(f"full_pool (completed games joined to game_features): {len(full_pool)}")
    print(f"  of those, rows with a non-null market_spread_home: {n_with_market}")

    n_cleared = 0
    n_test_nonempty = 0
    first_cleared_week = None
    for wk in weeks.itertuples():
        train_df = model.load_training_frame(con, before_date=wk.week_start)
        train_df = train_df.dropna(subset=["market_spread_home"])
        if len(train_df) < MIN_TRAIN_GAMES:
            continue
        n_cleared += 1
        if first_cleared_week is None:
            first_cleared_week = (wk.season, wk.week, len(train_df))
        test_mask = (full_pool["season"] == wk.season) & (full_pool["week"] == wk.week)
        test_df = full_pool[test_mask].dropna(subset=["market_spread_home"])
        if not test_df.empty:
            n_test_nonempty += 1

    print(f"\nweeks clearing the {MIN_TRAIN_GAMES}-training-game threshold: {n_cleared}")
    if first_cleared_week:
        print(f"  first one to clear it: season {first_cleared_week[0]} week {first_cleared_week[1]} "
              f"({first_cleared_week[2]} training games behind it)")
    print(f"of those, weeks with a non-empty (gradeable) test set: {n_test_nonempty}")

    con.close()


if __name__ == "__main__":
    main()
