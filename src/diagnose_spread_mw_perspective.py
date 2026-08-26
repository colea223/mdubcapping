"""
Follow-up to diagnose_spread_home_bias.py's per-team breakdown: no individual
MW home team stood out (max |z| = 1.46, Northern Illinois), and the skew was
broadly shared (7 of 10 teams positive) rather than concentrated -- ruling
out "one team's bad rating" and pointing at something structural about how
the model treats Mountain West games as a group.

Every residual/bias number so far has been computed from the HOME team's
perspective (+ = model over-predicted the home team's margin). For MW that
conflates two different situations: games where the MW team is hosting, and
games where the MW team is visiting a non-MW team. This script pulls those
apart by reframing the residual from the MW TEAM's OWN perspective instead
of always the home team's:

    mw_perspective_residual = predicted margin FOR the MW team
                               - actual margin FOR the MW team

For a home MW team this is just the usual residual as-is. For a visiting MW
team it's the sign-flipped version (an away team's own margin is the
negation of the home margin). An MW-vs-MW game contributes one observation
to EACH frame below (both participants are legitimately MW performances
worth evaluating on their own terms) -- so "MW as home" and "MW as away"
aren't a mutually-exclusive split of the same games, they're two angles that
can share some games.

This distinguishes two different explanations for the earlier finding:
  - If the bias is concentrated in "MW as home" games specifically, that
    points at home-field advantage being over-credited at MW venues (no
    explicit home-field feature exists in model.py -- it's implicit in the
    regression's intercept, calibrated on the whole P4-heavy national
    dataset, so a smaller true MW home boost would show up exactly like this).
  - If it shows up about equally whether MW is home or away, that points at
    something broader: the model under-rating Mountain West teams' actual
    competitiveness in general (their SP+/PPA/recruiting priors not
    translating cleanly from a P4-dominated training set), independent of
    which side of the field they're on.

Also reports win rate / ROI on the bets actually placed in each situation
(reusing backtest.py's own bet_result), to connect whichever pattern shows
up here back to real betting performance, not just raw residual size.

Usage:
    source .venv/bin/activate
    python src/diagnose_spread_mw_perspective.py
"""
import duckdb
import numpy as np
import pandas as pd

from config import DB_PATH
from backtest import run_backtest
from teams import MW_TEAMS_2026


def _residual_stats(df: pd.DataFrame, label: str, col="mw_perspective_residual"):
    n = len(df)
    if n == 0:
        print(f"  {label:<32} n=0")
        return
    mean_r = df[col].mean()
    median_r = df[col].median()
    std_r = df[col].std()
    se = std_r / np.sqrt(n) if n > 1 else float("nan")
    z = mean_r / se if se == se and se != 0 else float("nan")
    z_s = f"{z:+.2f}" if z == z else "--"
    print(f"  {label:<32} n={n:>6}  mean={mean_r:+.3f} pts  median={median_r:+.3f}  rough_z={z_s:>7}")


def _bet_stats(df: pd.DataFrame, label: str):
    bets = df[df["is_bet"] & df["bet_result"].isin(["Win", "Loss"])]
    wins = (bets["bet_result"] == "Win").sum()
    losses = (bets["bet_result"] == "Loss").sum()
    n = wins + losses
    if n == 0:
        print(f"  {label:<32} n_bets=0")
        return
    win_rate = wins / n
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n
    print(f"  {label:<32} n_bets={n:>6}  win_rate={win_rate * 100:>5.1f}%  roi={roi * 100:>+7.2f}%")


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_backtest(con)
    con.close()
    if df.empty:
        print("diagnose: not enough graded games yet -- run the full pipeline first.")
        return

    # pred_margin isn't stored directly -- model_spread_home = -pred_margin.
    df["pred_margin"] = -df["model_spread_home"]
    df["residual"] = df["pred_margin"] - df["actual_margin"]  # home-perspective, as in the other diagnose_*.py scripts

    home_is_mw = df["home_team"].isin(MW_TEAMS_2026)
    away_is_mw = df["away_team"].isin(MW_TEAMS_2026)

    mw_home_df = df[home_is_mw].copy()
    mw_home_df["mw_perspective_residual"] = mw_home_df["residual"]  # already MW's own perspective

    mw_away_df = df[away_is_mw].copy()
    mw_away_df["mw_perspective_residual"] = -mw_away_df["residual"]  # flip to the away MW team's own perspective

    both_mw_count = int((home_is_mw & away_is_mw).sum())
    print(f"(FYI: {both_mw_count} of these are MW-vs-MW games, contributing to BOTH frames below --")
    print(" that's deliberate; both participants' own performances are worth evaluating.)\n")

    print("=" * 72)
    print("Residual from the MW TEAM'S OWN perspective")
    print("+ = model over-predicted the MW team's own margin (bad for MW)")
    print("- = model under-predicted the MW team's own margin (MW outperformed)")
    print("=" * 72)
    _residual_stats(mw_home_df, "MW team is HOME")
    _residual_stats(mw_away_df, "MW team is AWAY")
    combined = pd.concat([mw_home_df["mw_perspective_residual"], mw_away_df["mw_perspective_residual"]])
    print(f"  {'Combined (every MW appearance)':<32} n={len(combined):>6}  mean={combined.mean():+.3f} pts  median={combined.median():+.3f}")
    print("\n  For reference: league-wide (all FBS) home-perspective residual was +0.190 pts,")
    print("  and MW-as-home-team residual (section 1 of the other script) was +1.299 pts.")
    print()

    print("=" * 72)
    print("Betting performance in each situation (whichever side the model leaned)")
    print("=" * 72)
    _bet_stats(mw_home_df, "MW team is HOME")
    _bet_stats(mw_away_df, "MW team is AWAY")


if __name__ == "__main__":
    main()
