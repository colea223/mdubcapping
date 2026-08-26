"""
Totals equivalent of diagnose_spread_bias.py + diagnose_spread_home_bias.py,
prompted by backtest.py's real totals numbers: win rate sits at 50.95%
overall / 51.2% MW (n=6,167 / 709) -- close to a coin flip, same shape as
spread's original problem -- but ROI is still negative both places (-2.73%
overall, -2.26% MW) because -110 needs 52.4% to break even. The real red
flag: mean CLV is -0.815 pts overall and -0.909 MW -- WORSE than spread's
pre-fix CLV (-0.66 / -0.49). A negative mean CLV across thousands of bets
means the closing total moved away from the side actually bet, on average --
that's not "no edge yet," that's a systematic direction problem, the same
category of finding that led to mw_involved_flag in model.py.

This script runs the same shapes of checks that worked for spread, adapted
for totals (Over/Under instead of Home/Away):
  1. Over-lean vs under-lean: does the model bet one side far more, and does
     that side lose more? (a "model thinks games run high/low" bias would
     show up as heavily Over- or Under-skewed bets with a low win rate on
     exactly those bets)
  2. Edge-size sweep: does win rate get WORSE as the model's claimed edge
     gets bigger? (sweep_total_threshold.py already does this for pace/tempo
     specifically; this repeats it generically against whatever totals model
     is live right now)
  3. Season-by-season: stable bias across 2016-2026, or concentrated in
     specific seasons?
  4. Whole-pool directional lean: among EVERY graded game (not just bets
     that clear the edge threshold), is model_total systematically higher or
     lower than the market on average? Most direct bias check -- independent
     of the threshold cutoff.
  5. Raw residual vs ACTUAL outcomes (residual = model_total - actual_total,
     completely independent of the market line). Mirrors
     diagnose_spread_home_bias.py's check 1 -- a model with no systematic
     bias should average close to 0 here across thousands of games.
  6. Does the residual track elevation_delta_away_ft? Thinner air affects
     kicking distance and (per some research) passing more than it affects
     which team wins by how much, so a totals-specific altitude effect is at
     least plausible even though the spread investigation ruled altitude out
     for MARGIN specifically -- worth checking independently rather than
     assuming the earlier verdict carries over.
  7. Residual by individual MW team (home OR away appearances combined,
     since a game's total isn't anchored to one team's side the way a
     spread's home-field credit is) -- flags whether the bias is
     concentrated in a few teams' games (offense/defense data or pace
     specific to them) or spread evenly across the conference (structural).

Reuses run_backtest() directly (same convention as every other diagnose_*.py
script) -- no CFBD API calls, this only reads what's already in the local
DuckDB database.

Usage:
    source .venv/bin/activate
    python src/diagnose_totals_bias.py
"""
import duckdb
import numpy as np
import pandas as pd

from config import DB_PATH
from backtest import run_backtest, EDGE_THRESHOLD
from teams import MW_TEAMS_2026

THRESHOLDS = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]

ELEV_BINS = [-float("inf"), -500, 0, 500, 1500, float("inf")]
ELEV_LABELS = [
    "<-500ft (away team descending)", "-500 to 0ft", "0 to 500ft",
    "500 to 1500ft", ">1500ft (big altitude jump for away team)",
]


def _win_rate_roi(bets: pd.DataFrame, result_col: str):
    graded = bets[bets[result_col].isin(["Win", "Loss"])]
    wins = (graded[result_col] == "Win").sum()
    losses = (graded[result_col] == "Loss").sum()
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
        win_rate, roi, n = _win_rate_roi(sl, "total_bet_result")
        mean_clv = sl.loc[sl["total_bet_result"].isin(["Win", "Loss"]), "total_clv"].mean() if n else None
        wr_s = f"{win_rate * 100:.1f}%" if win_rate is not None else "--"
        roi_s = f"{roi * 100:+.2f}%" if roi is not None else "--"
        clv_s = f"{mean_clv:+.3f}" if mean_clv is not None and pd.notna(mean_clv) else "--"
        print(f"  {label:<12} n_bets={n:>6}  win_rate={wr_s:>7}  roi={roi_s:>8}  mean_clv={clv_s:>8}")
    print()


def _residual_stats(df: pd.DataFrame, label: str):
    n = len(df)
    if n == 0:
        print(f"  {label:<32} n=0")
        return
    mean_resid = df["residual"].mean()
    median_resid = df["residual"].median()
    print(f"  {label:<32} n={n:>6}  mean_residual={mean_resid:+.3f} pts  median={median_resid:+.3f} pts")


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_backtest(con)
    if df.empty:
        print("diagnose: not enough graded games yet -- run the full pipeline first.")
        con.close()
        return

    # residual = model_total - actual_total, independent of the market line.
    # + = model predicted a higher-scoring game than actually happened.
    graded_totals = df.dropna(subset=["model_total", "actual_total"]).copy()
    graded_totals["residual"] = graded_totals["model_total"] - graded_totals["actual_total"]

    elev = con.execute("SELECT game_id, elevation_delta_away_ft FROM game_features").fetchdf()
    con.close()
    graded_totals = graded_totals.merge(elev, on="game_id", how="left")

    for scope_label, scope_df in [
        ("Overall (all FBS)", df),
        ("Mountain West-involved", df[df["is_mw_game"]]),
    ]:
        print(f"\n{'=' * 70}\n{scope_label}\n{'=' * 70}\n")

        bets = scope_df[scope_df["is_total_bet"] == True].copy()

        # 1. Over-lean vs under-lean
        _report_split(bets, "total_lean", {"Over": "Over lean", "Under": "Under lean"},
                      "Over-lean vs under-lean")

        # 2. Edge-size sweep -- same recompute-from-raw-columns approach as
        # diagnose_spread_bias.py and sweep_total_threshold.py, so it isn't
        # locked to backtest.py's single EDGE_THRESHOLD.
        print(f"--- Edge-size sweep (backtest.py's own threshold is {EDGE_THRESHOLD}) ---")
        for threshold in THRESHOLDS:
            sub = scope_df.dropna(subset=["total_edge", "actual_total", "market_total"])
            sub = sub[sub["total_edge"].abs() >= threshold]
            if sub.empty:
                print(f"  threshold {threshold:>4.1f}: n_bets=     0")
                continue
            lean_over = sub["total_edge"] > 0
            push = sub["actual_total"] == sub["market_total"]
            actual_over = sub["actual_total"] > sub["market_total"]
            win = (~push) & (lean_over == actual_over)
            loss = (~push) & (lean_over != actual_over)
            wins, losses = int(win.sum()), int(loss.sum())
            n = wins + losses
            wr = f"{wins / n * 100:.1f}%" if n else "--"
            roi = f"{(wins * (100 / 110) - losses) / n * 100:+.2f}%" if n else "--"
            print(f"  threshold {threshold:>4.1f}: n_bets={n:>6}  win_rate={wr:>7}  roi={roi:>8}")
        print()

        # 3. Season-by-season stability
        print("--- Win rate by season ---")
        for season, sl in bets.groupby("season"):
            win_rate, roi, n = _win_rate_roi(sl, "total_bet_result")
            if n == 0:
                continue
            print(f"  {season}: n_bets={n:>5}  win_rate={win_rate * 100:>5.1f}%  roi={roi * 100:>+6.2f}%")
        print()

        # 4. Whole-pool directional lean -- every graded game, no threshold.
        pool = scope_df.dropna(subset=["total_edge"])
        mean_edge = pool["total_edge"].mean()
        pct_over_lean = (pool["total_lean"] == "Over").mean() * 100
        print(f"--- Whole-pool directional check (all {len(pool)} graded games, no threshold) ---")
        print(f"  mean edge (model_total - market_total): {mean_edge:+.3f} pts")
        print(f"  % of games where model leans Over: {pct_over_lean:.1f}% (50% = no directional skew)")
        print()

        # 5. Raw residual vs actual outcomes, independent of the market.
        scope_resid = graded_totals if scope_label.startswith("Overall") else graded_totals[graded_totals["is_mw_game"]]
        print("--- Raw residual vs ACTUAL outcomes (independent of the market line) ---")
        print("    + = model predicted a higher-scoring game than actually happened")
        _residual_stats(scope_resid, scope_label)
        print()

    # 6. Elevation correlation -- checked across the whole graded pool once,
    # not per Overall/MW loop above (same structure as diagnose_spread_home_bias.py).
    print("=" * 70)
    print("Does the totals residual track elevation_delta_away_ft?")
    print("=" * 70)
    elev_graded = graded_totals.dropna(subset=["elevation_delta_away_ft", "residual"])
    corr = elev_graded["elevation_delta_away_ft"].corr(elev_graded["residual"])
    print(f"  Correlation, all FBS (n={len(elev_graded)}): {corr:+.4f}")
    mw_elev = elev_graded[elev_graded["is_mw_game"]]
    corr_mw = mw_elev["elevation_delta_away_ft"].corr(mw_elev["residual"])
    print(f"  Correlation, MW-involved (n={len(mw_elev)}): {corr_mw:+.4f}")
    print("  (positive = the model predicts a HIGHER total the bigger the away team's")
    print("   altitude jump -- i.e. over-crediting scoring at altitude; negative = the")
    print("   model under-predicts scoring at altitude, e.g. missing a thin-air kicking/")
    print("   passing boost)")
    print()
    elev_graded = elev_graded.copy()
    elev_graded["elev_bucket"] = pd.cut(elev_graded["elevation_delta_away_ft"], bins=ELEV_BINS, labels=ELEV_LABELS)
    print("  Mean residual by elevation_delta_away_ft bucket (all FBS):")
    for label, sl in elev_graded.groupby("elev_bucket", observed=True):
        if len(sl) == 0:
            continue
        print(f"    {label:<42} n={len(sl):>6}  mean_residual={sl['residual'].mean():+.3f} pts")
    print()
    mw_elev_b = mw_elev.copy()
    mw_elev_b["elev_bucket"] = pd.cut(mw_elev_b["elevation_delta_away_ft"], bins=ELEV_BINS, labels=ELEV_LABELS)
    print("  Mean residual by elevation_delta_away_ft bucket (MW-involved):")
    for label, sl in mw_elev_b.groupby("elev_bucket", observed=True):
        if len(sl) == 0:
            continue
        print(f"    {label:<42} n={len(sl):>6}  mean_residual={sl['residual'].mean():+.3f} pts")
    print()

    # 7. Residual by individual MW team -- home OR away appearances combined,
    # since a game's total isn't anchored to one team's side the way a
    # spread's home-field credit is.
    print("=" * 70)
    print("Residual by individual MW team (every game they appear in, home or away)")
    print("=" * 70)
    rows = []
    for team in MW_TEAMS_2026:
        team_games = graded_totals[(graded_totals["home_team"] == team) | (graded_totals["away_team"] == team)]
        if team_games.empty:
            continue
        n = len(team_games)
        mean_r = team_games["residual"].mean()
        median_r = team_games["residual"].median()
        std_r = team_games["residual"].std()
        se = std_r / np.sqrt(n) if n > 1 else float("nan")
        z = mean_r / se if se == se and se != 0 else float("nan")
        rows.append((team, n, mean_r, median_r, std_r, z))
    rows.sort(key=lambda r: r[2], reverse=True)
    print(f"  {'Team':<20} {'n':>5} {'mean_resid':>11} {'median':>8} {'std':>7} {'rough_z':>8}")
    for team, n, mean_r, median_r, std_r, z in rows:
        z_s = f"{z:+.2f}" if z == z else "--"
        print(f"  {team:<20} {n:>5} {mean_r:>+10.3f}  {median_r:>+7.3f} {std_r:>7.2f} {z_s:>8}")
    print()
    print("  |rough_z| roughly above ~2 is worth a closer look; treat this as a flag, not")
    print("  a verdict -- a team's own games aren't independent draws (pace/scheme/injury")
    print("  context persists across its season), so this overstates real confidence somewhat.")


if __name__ == "__main__":
    main()
