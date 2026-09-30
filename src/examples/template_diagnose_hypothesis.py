"""
TEMPLATE -- a fill-in-the-blanks skeleton for testing ANY hypothesis about
the model with real data, before changing any production code. This is the
exact shape src/diagnose_spread_bias.py, diagnose_situational_features.py,
and diagnose_training_window.py all follow -- copy it, rename it
diagnose_<your question>.py, and fill in the four marked sections.

WHY THIS PATTERN EXISTS: it is very tempting to "just try" a change (add a
feature, retune a threshold, swap a model) directly in the live pipeline
and see if this week's picks look better. That's how you fool yourself --
one good or bad week is noise, not signal (see PART 2 of the improvement
playbook doc this file ships alongside). Every real change in this project
went through a standalone diagnose_*.py script FIRST: run it against the
real database, read a real walk-forward accuracy number, THEN decide
whether to touch model.py/features.py/predict_week.py at all. This script
never gets wired into run_pipeline.py -- it's meant to be run by hand,
read once, and archived (or deleted) once you have your answer. Some
diagnose_*.py scripts stick around because Cole keeps re-checking the same
question each season (diagnose_spread_bias.py); most are one-shot.

THE FOUR SECTIONS TO FILL IN:
  1. YOUR HYPOTHESIS -- one sentence, in the docstring, stating exactly
     what you expect to see and why. If you can't state it before running
     anything, you don't have a hypothesis yet, you have a hunch -- that's
     fine, but write the hunch down anyway so you know what you were
     testing when you read the numbers later.
  2. THE DATA YOU NEED -- almost always model.load_training_frame(con)
     (every completed game with features + market lines) or a filtered
     slice of it. Don't hand-query game_features yourself unless you have
     a specific reason to bypass load_training_frame()'s existing joins.
  3. THE COMPARISON -- what are you actually holding constant, and what
     are you varying? (diagnose_training_window.py varies the training
     window and holds the model architecture + test weeks constant.
     diagnose_spread_bias.py varies nothing and just measures a slice.)
  4. THE GRADE -- always run any spread prediction through
     backtest.grade_spread_pick(), never write your own win/loss logic.
     That function encodes real decisions (the favorite-default rule, the
     Push/Win/Loss boundary, the edge_threshold convention) that are easy
     to get subtly wrong in a one-off reimplementation, and a diagnostic
     that grades differently from the rest of the project produces a
     number nobody can trust or compare.

Usage:
    source .venv/bin/activate
    python src/examples/template_diagnose_hypothesis.py
"""
import time

import duckdb
import numpy as np
import pandas as pd

from config import DB_PATH
import model
from teams import MW_TEAMS_2026
from backtest import _test_weeks, EDGE_THRESHOLD, MIN_TRAIN_GAMES, grade_spread_pick

# --------------------------------------------------------------------------
# SECTION 1: YOUR HYPOTHESIS
# --------------------------------------------------------------------------
# Example (delete and replace with your own): "Games with a rest_diff of 3+
# days are systematically under-priced by the market -- Ridge should show a
# real, sustained edge on that slice specifically, not just noise."
HYPOTHESIS = "REPLACE ME: state exactly what you expect and why, one sentence."


def load_relevant_games(con):
    """
    SECTION 2: THE DATA YOU NEED. Default here is every graded game the
    live walk-forward loop would ever see -- narrow this with a pandas
    filter, not a rewritten SQL query, so you inherit load_training_frame()'s
    existing leakage-safe joins unchanged.
    """
    df = model.load_training_frame(con)
    df = df.dropna(subset=["market_spread_home"])
    # Example filter (delete/replace): only games with a real rest_diff --
    # df = df[df["rest_diff"].abs() >= 3]
    return df


def compare(con, edge_threshold=EDGE_THRESHOLD):
    """
    SECTION 3: THE COMPARISON. This default skeleton just measures Ridge's
    walk-forward record on your filtered slice vs. the whole pool, using
    the SAME walk-forward discipline every other script in this project
    uses (train on everything strictly before a test week, grade only that
    week, move on) -- see backtest.py's own docstring for why that
    discipline is non-negotiable (anything else lets a model "predict" a
    game using data from after it was played).
    """
    weeks = _test_weeks(con)
    full_pool = load_relevant_games(con).set_index("game_id")

    rows = []
    for idx, wk in enumerate(weeks.itertuples(), start=1):
        if idx % 20 == 0:
            print(f"  ...{idx}/{len(weeks)} candidate weeks processed", flush=True)
        train_df = model.load_training_frame(con, before_date=wk.week_start)
        train_df = train_df.dropna(subset=["market_spread_home"])
        if len(train_df) < MIN_TRAIN_GAMES:
            continue

        test_mask = (full_pool["season"] == wk.season) & (full_pool["week"] == wk.week)
        test_df = full_pool[test_mask]
        if test_df.empty:
            continue

        pipe, _ = model.fit_margin_model(train_df)
        pred_margin = model.predict_margin(pipe, test_df)
        spread_home = -pred_margin

        for i, (game_id, row) in enumerate(test_df.iterrows()):
            # SECTION 4: THE GRADE -- always through the shared function.
            grade = grade_spread_pick(
                spread_home[i], row["market_spread_home"], None, row["margin"], edge_threshold
            )
            rows.append({
                "game_id": game_id, "season": row["season"], "week": row["week"],
                "is_mw_game": row["home_team"] in MW_TEAMS_2026 or row["away_team"] in MW_TEAMS_2026,
                "edge": grade["edge"], "lean": grade["lean"],
                "is_bet": grade["is_bet"], "result": grade["bet_result"],
            })

    return pd.DataFrame(rows)


def _win_rate_roi(df):
    bets = df[df["result"].isin(["Win", "Loss"])]
    wins = (bets["result"] == "Win").sum()
    n = len(bets)
    win_rate = wins / n if n else float("nan")
    roi = (wins * (100 / 110) - (n - wins)) / n if n else float("nan")
    return win_rate, roi, n


def main():
    print(f"Hypothesis: {HYPOTHESIS}\n")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    df = compare(con)
    con.close()

    if df.empty:
        print("Not enough graded games in this slice to say anything yet.")
        return

    bets = df[df["is_bet"] == True]  # real-edge bets only -- see grade_spread_pick's own docstring
    win_rate, roi, n = _win_rate_roi(bets)
    print(f"Real-edge bets on this slice: n={n}  win_rate={win_rate * 100:.1f}%  roi={roi * 100:+.2f}%")

    all_win_rate, all_roi, all_n = _win_rate_roi(df)
    print(f"Every graded game on this slice (no edge threshold): n={all_n}  "
          f"win_rate={all_win_rate * 100:.1f}%  roi={all_roi * 100:+.2f}%")

    print(
        "\nReading this: n needs to be large before you trust win_rate at all -- a 55% win rate on "
        "40 bets is comfortably within coin-flip noise (see the sample-size note in the improvement "
        "playbook's Golden Rule section). Compare this slice's numbers against the SAME model's whole-"
        "pool numbers (run backtest.py or model_comparison.py) before concluding the slice is special."
    )


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
