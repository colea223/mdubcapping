"""
Diagnostic for the sweep_total_threshold.py finding: MW total ROI under the
new SP+/PPA model is decent (~-1.4% to -1.8%) at thresholds 1.5-3.0, then
gets sharply worse (-4.24% to -6.02%) above 3.0. That's backwards from the
usual pattern (a bigger claimed edge should be a MORE trustworthy bet, not
less) -- this script tests the most likely explanation.

Theory: the three teams new to the Mountain West / new to FBS entirely --
North Dakota State (zero FBS history anywhere), Northern Illinois, and UTEP
(both full FBS history, but none of it in the MW) -- have prior-season
SP+/PPA priors that are either missing (NDSU) or drawn from a totally
different competitive level (NIU's MAC history, UTEP's C-USA history) than
what they're now facing in the MW. totals_model.py leans on exactly those
priors. A prior that's either absent or miscalibrated for the new level of
competition can push the model's prediction unusually far from the market
number -- precisely the kind of "big edge" that would concentrate in the
high-threshold buckets that turned out to be the worst bets, not the best.
teams.py already flags these three via MW_TEAMS_2026[...]["joined_2026"].

Reuses run_backtest()'s output directly (no dependency on a possibly-stale
backtest_results.csv) and summarize_totals_at() from sweep_total_threshold.py,
splitting MW games by whether either side is one of these three teams.

Usage:
    source .venv/bin/activate
    python src/diagnose_totals_newcomers.py
"""
import duckdb
import time

from config import DB_PATH
from backtest import run_backtest
from sweep_total_threshold import summarize_totals_at
from teams import MW_TEAMS_2026

NEWCOMER_TEAMS = {team for team, meta in MW_TEAMS_2026.items() if meta["joined_2026"]}
THRESHOLDS = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0]


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_backtest(con)
    con.close()

    if df.empty:
        print("diagnose: not enough graded games yet -- run the full pipeline first.")
        return

    mw = df[df["is_mw_game"]]
    is_newcomer = mw["home_team"].isin(NEWCOMER_TEAMS) | mw["away_team"].isin(NEWCOMER_TEAMS)
    newcomer_games = mw[is_newcomer]
    other_mw_games = mw[~is_newcomer]

    print(f"Newcomer teams tracked: {sorted(NEWCOMER_TEAMS)}")
    print(f"MW games total: {len(mw)} -- {len(newcomer_games)} involve a newcomer, {len(other_mw_games)} don't.\n")

    header = f"{'Threshold':>9} | {'Group':<20} | {'N Bets':>7} | {'Win Rate':>9} | {'ROI (flat 1u)':>14}"
    print(header)
    print("-" * len(header))
    for threshold in THRESHOLDS:
        for label, sl in [("Newcomer-involved", newcomer_games), ("Other MW", other_mw_games)]:
            s = summarize_totals_at(sl, threshold, label)
            wr = f"{s['win_rate'] * 100:.1f}%" if s.get("win_rate") is not None else "--"
            roi = f"{s['roi_flat_stake'] * 100:+.2f}%" if s.get("roi_flat_stake") is not None else "--"
            print(f"{threshold:>9.1f} | {label:<20} | {s.get('n_bets', 0):>7} | {wr:>9} | {roi:>14}")
        print()

    # If the newcomer share of bets climbs sharply at higher thresholds,
    # that's the smoking gun -- it means the model's biggest "edges" are
    # disproportionately teams it has the least real information about.
    print("Newcomer share of MW bets by threshold (a rising share points at the newcomers as the cause):")
    for threshold in THRESHOLDS:
        bettable = mw[mw["total_edge"].abs() >= threshold]
        if bettable.empty:
            print(f"  {threshold:>4.1f}: no bets")
            continue
        pct = 100 * (bettable["home_team"].isin(NEWCOMER_TEAMS) | bettable["away_team"].isin(NEWCOMER_TEAMS)).mean()
        print(f"  {threshold:>4.1f}: {pct:.1f}% of {len(bettable)} bettable MW games involve a newcomer team")


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
