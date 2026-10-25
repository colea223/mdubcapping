"""
Walk-forward test of how fast Dub Gamma should forget its Moore preseason seed
(and whether to cap blowout margins). READ-ONLY on the database.

Gamma only exists for 2026, so the sample is ~one season of games. The primary
yardstick is therefore ACCURACY (mean absolute error of the predicted margin vs
the actual margin, and vs the market line), not ATS% -- ATS over a few hundred
games is mostly noise. Results are shown for early weeks (2-5), later weeks
(6+), and every week.

Settings tested (everything else, including travel/injury adjustments, is held
at the raw rating line so the comparison is clean):
  * constant update weight: 0.10 (current), 0.15, 0.20, 0.25
  * early-weeks boost: weight 0.20 or 0.30 through week 4 or 6, then 0.10
  * blowout cap on the margin used in a rating update: none, 35, 28, 21

Usage:
    python src/diagnose_gamma_fade.py
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DB_PATH  # noqa: E402
import backtest  # noqa: E402
import gamma_model  # noqa: E402
from teams import normalize_team_name  # noqa: E402

SEASON = 2026

CONFIGS = [("current (0.10, no cap)", dict(w=0.10, early=None, early_weeks=0, cap=None))]
CONFIGS += [(f"constant {w:.2f}", dict(w=w, early=None, early_weeks=0, cap=None)) for w in (0.15, 0.20, 0.25)]
CONFIGS += [(f"early {e:.2f} thru wk{k}, then 0.10", dict(w=0.10, early=e, early_weeks=k, cap=None))
            for e in (0.20, 0.30) for k in (4, 6)]
CONFIGS += [(f"cap {c} (0.10)", dict(w=0.10, early=None, early_weeks=0, cap=c)) for c in (35, 28, 21)]
CONFIGS += [("early 0.20 thru wk4 + cap 28", dict(w=0.10, early=0.20, early_weeks=4, cap=28))]


def set_config(cfg):
    gamma_model.PERFORMANCE_WEIGHT = cfg["w"]
    gamma_model.EARLY_PERFORMANCE_WEIGHT = cfg["early"]
    gamma_model.EARLY_WEEKS = cfg["early_weeks"]
    gamma_model.NET_SCORE_CAP = cfg["cap"]


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    games = con.execute("""
        SELECT g.game_id, g.week, g.home_team, g.away_team, g.home_points, g.away_points, g.neutral_site,
               m.market_spread_home
        FROM games g
        LEFT JOIN (SELECT game_id, AVG(spread) AS market_spread_home FROM lines GROUP BY game_id) m
               ON m.game_id = g.game_id
        WHERE g.season = ? AND g.season_type = 'regular' AND g.completed = TRUE
          AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
        ORDER BY g.week
    """, [SEASON]).fetchdf()
    weeks = sorted(w for w in games["week"].unique() if w >= 2)
    print(f"{len(games)} completed {SEASON} games, weeks {weeks[0]}-{weeks[-1]}. Running {len(CONFIGS)} settings...")

    saved = (gamma_model.PERFORMANCE_WEIGHT, gamma_model.EARLY_PERFORMANCE_WEIGHT,
             gamma_model.EARLY_WEEKS, gamma_model.NET_SCORE_CAP)
    out = []
    try:
        for label, cfg in CONFIGS:
            set_config(cfg)
            rows = []
            for wk in weeks:
                ratings, _ = gamma_model.replay_ratings(con, season=SEASON, through_week=int(wk) - 1)
                ratings = dict(ratings)     # raw rating line, no travel/injury adjustment
                for r in games[games["week"] == wk].itertuples():
                    h, a = normalize_team_name(r.home_team), normalize_team_name(r.away_team)
                    if h not in ratings or a not in ratings:
                        continue
                    spread = gamma_model.predict_spread_home(ratings, h, a, neutral_site=bool(r.neutral_site))
                    margin = r.home_points - r.away_points
                    mk = r.market_spread_home
                    res = None
                    if pd.notna(mk):
                        res = backtest.grade_spread_pick(float(spread), float(mk), None, float(margin),
                                                         backtest.EDGE_THRESHOLD)["bet_result"]
                    rows.append({"week": wk, "err": abs(-spread - margin),
                                 "gap_to_market": abs(spread - mk) if pd.notna(mk) else np.nan, "res": res})
            df = pd.DataFrame(rows)

            def stats(d):
                dec = d[d["res"].isin(["Win", "Loss"])]
                return (round(d["err"].mean(), 2), round(d["gap_to_market"].mean(), 2),
                        round(100 * (dec["res"] == "Win").mean(), 1) if len(dec) else np.nan)
            e_all, g_all, a_all = stats(df)
            e_early, _, a_early = stats(df[df["week"] <= 5])
            e_late, _, a_late = stats(df[df["week"] >= 6])
            out.append({"setting": label, "MAE_all": e_all, "MAE_wk2-5": e_early, "MAE_wk6+": e_late,
                        "gap_to_mkt": g_all, "ATS%_all": a_all, "ATS%_wk2-5": a_early, "ATS%_wk6+": a_late})
            print(f"  done: {label}")
    finally:
        (gamma_model.PERFORMANCE_WEIGHT, gamma_model.EARLY_PERFORMANCE_WEIGHT,
         gamma_model.EARLY_WEEKS, gamma_model.NET_SCORE_CAP) = saved

    res = pd.DataFrame(out)
    print("\n" + res.to_string(index=False))
    base = res.iloc[0]
    print(f"\nBaseline MAE {base['MAE_all']} (all weeks), {base['MAE_wk2-5']} (wk2-5), {base['MAE_wk6+']} (wk6+).")
    print("Lower MAE = better. Prefer a setting that improves MAE in BOTH the early and later slices;")
    print("ignore differences under ~0.1 point of MAE -- that is within noise for one season.")


if __name__ == "__main__":
    main()
