"""
Bias diagnostic for the spread model, prompted by backtest.py's real numbers:
ATS win rate sits at 49.45% overall / 49.54% MW (n=6,538 / 757) -- both BELOW
even a coin flip, well below the 52.4% needed to break even at -110 -- and
mean CLV is negative both places (-0.66 pts overall, -0.49 MW). A large
enough sample landing below 50% isn't just "no edge yet," and negative CLV
specifically is a different kind of signal than a merely noisy model: it
means the closing number moved AWAY from the side actually bet, on average,
across thousands of bets. A model with zero real skill should have CLV
scattered around zero, not consistently negative.

This script checks the most likely SHAPES a systematic bias could take,
before assuming the fix is "add more features":
  1. Home-lean vs away-lean: does the model bet one side far more than the
     other, and does that side lose more? (a home-team-optimism bias would
     show up as heavily home-skewed bets with a low win rate on exactly
     those bets)
  2. Favorite-lean vs underdog-lean: same idea, but split by whether the
     model's pick agrees with the market on WHO's better (picking the
     favorite to cover more/less) or is contrarian (picking the dog).
  3. Edge-size sweep: does win rate get WORSE as the model's claimed edge
     gets bigger? (exactly the pattern totals showed for MW at high
     thresholds before -- "more confident" should mean "more right," and if
     it's backwards here too that's a real structural problem, not noise)
  4. Season-by-season: is this a stable bias across the whole 2016-2026
     window, or concentrated in specific seasons (which would point at a
     data issue in those years rather than a model-wide problem)?
  5. Whole-pool directional lean: among EVERY graded game (not just the ones
     that clear the betting threshold), is model_spread_home systematically
     more home-favorable or away-favorable than the market on average? This
     is the most direct bias check -- it isolates whether the model itself
     is shaded one direction, independent of the threshold cutoff.

Reuses run_backtest() directly (same convention as diagnose_totals_newcomers.py).

Usage:
    source .venv/bin/activate
    python src/diagnose_spread_bias.py
"""
import duckdb
import pandas as pd

from config import DB_PATH
from backtest import run_backtest, EDGE_THRESHOLD

THRESHOLDS = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]


def _win_rate_roi(bets: pd.DataFrame):
    graded = bets[bets["bet_result"].isin(["Win", "Loss"])]
    wins = (graded["bet_result"] == "Win").sum()
    losses = (graded["bet_result"] == "Loss").sum()
    n = wins + losses
    if n == 0:
        return None, None, 0
    win_rate = wins / n
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n
    return win_rate, roi, n


def _report_split(df: pd.DataFrame, split_col: str, label_for: dict, title: str):
    print(f"--- {title} ---")
    for val, label in label_for.items():
        sl = df[df[split_col] == val]
        win_rate, roi, n = _win_rate_roi(sl)
        mean_clv = sl.loc[sl["bet_result"].isin(["Win", "Loss"]), "clv"].mean() if n else None
        wr_s = f"{win_rate * 100:.1f}%" if win_rate is not None else "--"
        roi_s = f"{roi * 100:+.2f}%" if roi is not None else "--"
        clv_s = f"{mean_clv:+.3f}" if mean_clv is not None and pd.notna(mean_clv) else "--"
        print(f"  {label:<12} n_bets={n:>6}  win_rate={wr_s:>7}  roi={roi_s:>8}  mean_clv={clv_s:>8}")
    print()


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_backtest(con)
    con.close()

    if df.empty:
        print("diagnose: not enough graded games yet -- run the full pipeline first.")
        return

    for scope_label, scope_df in [("Overall (all FBS)", df), ("Mountain West-involved", df[df["is_mw_game"]])]:
        print(f"\n{'=' * 70}\n{scope_label}\n{'=' * 70}\n")

        bets = scope_df[scope_df["is_bet"]]

        # 1. Home-lean vs away-lean
        _report_split(bets, "lean", {"Home": "Home lean", "Away": "Away lean"},
                      "Home-lean vs away-lean")

        # 2. Favorite-lean vs underdog-lean. "Favorite" = the model's pick is
        # also the side the MARKET already favors (market_spread_home < 0
        # means home favored, so a Home lean there is "agreeing with the
        # favorite"; an Away lean there is "backing the market's underdog").
        is_favorite_pick = (
            ((bets["lean"] == "Home") & (bets["market_spread_home"] < 0)) |
            ((bets["lean"] == "Away") & (bets["market_spread_home"] > 0))
        )
        bets = bets.copy()
        bets["pick_type"] = is_favorite_pick.map({True: "favorite", False: "underdog"})
        _report_split(bets, "pick_type", {"favorite": "Model backs fav", "underdog": "Model backs dog"},
                      "Favorite-lean vs underdog-lean")

        # 3. Edge-size sweep -- same shape as sweep_total_threshold.py, applied
        # to spread edge instead. Recomputed directly from edge/actual_margin/
        # market_spread_home so it isn't locked to backtest.py's single
        # EDGE_THRESHOLD.
        print(f"--- Edge-size sweep (backtest.py's own threshold is {EDGE_THRESHOLD}) ---")
        for threshold in THRESHOLDS:
            sub = scope_df[scope_df["edge"].abs() >= threshold].copy()
            lean = pd.Series("Home", index=sub.index)
            lean[sub["edge"] < 0] = "Away"
            cover_value = sub["actual_margin"] + sub["market_spread_home"]
            home_covers = pd.Series(None, index=sub.index, dtype="object")
            home_covers[cover_value > 0] = True
            home_covers[cover_value < 0] = False
            graded = sub[home_covers.notna()]
            lean_g = lean[graded.index]
            result_win = (lean_g == "Home") == home_covers[graded.index]
            wins = int(result_win.sum())
            losses = int((~result_win).sum())
            n = wins + losses
            wr = f"{wins / n * 100:.1f}%" if n else "--"
            roi = f"{(wins * (100 / 110) - losses) / n * 100:+.2f}%" if n else "--"
            print(f"  threshold {threshold:>4.1f}: n_bets={n:>6}  win_rate={wr:>7}  roi={roi:>8}")
        print()

        # 4. Season-by-season stability
        print("--- Win rate by season ---")
        for season, sl in bets.groupby("season"):
            win_rate, roi, n = _win_rate_roi(sl)
            if n == 0:
                continue
            print(f"  {season}: n_bets={n:>5}  win_rate={win_rate * 100:>5.1f}%  roi={roi * 100:>+6.2f}%")
        print()

        # 5. Whole-pool directional lean -- every graded game, not just bets.
        # A systematic shade toward one side shows up here even before any
        # threshold is applied.
        mean_edge = scope_df["edge"].mean()
        pct_home_lean = (scope_df["lean"] == "Home").mean() * 100
        print(f"--- Whole-pool directional check (all {len(scope_df)} graded games, no threshold) ---")
        print(f"  mean edge (market_spread_home - model_spread_home): {mean_edge:+.3f} pts")
        print(f"  % of games where model leans Home: {pct_home_lean:.1f}% (50% = no directional skew)")
        print()


if __name__ == "__main__":
    main()
