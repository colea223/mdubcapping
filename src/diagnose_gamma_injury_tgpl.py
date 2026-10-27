"""
Does counting injuries in Gamma's "true game performance" (the rating UPDATE after each game) help or hurt
spread accuracy? READ-ONLY on the database.

Compares, over the most recent N completed weeks of the season (walk-forward: each week is predicted from
ratings built only from earlier weeks):

  A  injuries IN the rating update (current model)       vs   B  injuries OUT of the rating update

and, separately for each, with and without the pre-game spread adjustments (travel + injury burden):

  raw line            = away rating - home rating - HFA
  adjusted line       = raw line + travel + injury adjustments (what the live model shows)

Metrics: mean absolute error of the predicted margin vs the real margin, mean gap to the market line,
and ATS% (backtest.grade_spread_pick, so the 1-point Vegas-favorite default applies).
Also prints the current top-12 ratings and the biggest rating differences between A and B (e.g. South Carolina).

Usage:
    python src/diagnose_gamma_injury_tgpl.py            # last 6 weeks
    python src/diagnose_gamma_injury_tgpl.py 8          # last 8 weeks
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


def main():
    n_weeks = int(sys.argv[1]) if len(sys.argv) > 1 else 6
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
    games = games[games["market_spread_home"].notna()]
    all_weeks = sorted(w for w in games["week"].unique() if w >= 2)
    weeks = all_weeks[-n_weeks:]
    print(f"Testing weeks {weeks[0]}-{weeks[-1]} ({n_weeks} weeks) of {SEASON}.")

    # Build the REAL travel/injury adjustment tables once and cache them, so the adjustments stay identical
    # in both variants even while the rating-update injury term is switched off below.
    gamma_model._adjustment_tables(con, SEASON)
    real_fn = gamma_model.load_injury_weights_by_week

    def replay(use_injuries, through_week):
        gamma_model.load_injury_weights_by_week = real_fn if use_injuries else (lambda c, s: {})
        try:
            return gamma_model.replay_ratings(con, season=SEASON, through_week=through_week)[0]
        finally:
            gamma_model.load_injury_weights_by_week = real_fn

    rows = []
    for wk in weeks:
        for tgpl_inj in (True, False):
            ratings = replay(tgpl_inj, int(wk) - 1)
            plain = dict(ratings)
            for r in games[games["week"] == wk].itertuples():
                h, a = normalize_team_name(r.home_team), normalize_team_name(r.away_team)
                if h not in plain or a not in plain:
                    continue
                margin = r.home_points - r.away_points
                for adj_on in (False, True):
                    src = ratings if adj_on else plain
                    spread = gamma_model.predict_spread_home(src, h, a, neutral_site=bool(r.neutral_site))
                    res = backtest.grade_spread_pick(float(spread), float(r.market_spread_home), None,
                                                     float(margin), backtest.EDGE_THRESHOLD)["bet_result"]
                    rows.append({"tgpl_injuries": "IN (current)" if tgpl_inj else "OUT",
                                 "spread_adjustments": "on" if adj_on else "off",
                                 "week": wk, "game_id": r.game_id, "err": abs(-spread - margin),
                                 "gap": abs(spread - r.market_spread_home), "res": res})
        print(f"  week {wk} done")
    df = pd.DataFrame(rows)

    out = []
    for (t, s), g in df.groupby(["tgpl_injuries", "spread_adjustments"]):
        dec = g[g["res"].isin(["Win", "Loss"])]
        out.append({"injuries in rating update": t, "spread adjustments": s, "games": len(dec),
                    "MAE_margin": round(g["err"].mean(), 2), "gap_to_market": round(g["gap"].mean(), 2),
                    "ATS%": round(100 * (dec["res"] == "Win").mean(), 1)})
    print("\n" + pd.DataFrame(out).sort_values(["spread adjustments", "injuries in rating update"]).to_string(index=False))
    print("\nLower MAE = better. Differences under ~0.1 pt of MAE or ~2 pts of ATS% are within noise "
          f"for ~{len(df) // 4} games.")

    # Current ratings both ways
    a = replay(True, None)
    b = replay(False, None)
    cmp = pd.DataFrame({"with_injuries": pd.Series(dict(a)), "without": pd.Series(dict(b))}).dropna()
    cmp["diff"] = cmp["with_injuries"] - cmp["without"]
    cmp["rank_with"] = cmp["with_injuries"].rank(ascending=False).astype(int)
    cmp["rank_without"] = cmp["without"].rank(ascending=False).astype(int)
    print("\nCurrent top 12 (injuries in rating update):")
    print(cmp.sort_values("with_injuries", ascending=False).head(12).round(1).to_string())
    print("\nBiggest rating differences caused by the injury term:")
    print(cmp.reindex(cmp["diff"].abs().sort_values(ascending=False).index).head(10).round(1).to_string())


if __name__ == "__main__":
    main()
