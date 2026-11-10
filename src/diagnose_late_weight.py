"""
Should Gamma learn faster from results later in the season? And how much should it be pulled toward the market?
READ-ONLY on the database.

Part 1 - late-season update weight: walk-forward on completed 2026 games, comparing the current flat 0.10 weight with
          a higher weight for games from week S onward (gamma_model.LATE_PERFORMANCE_WEIGHT / LATE_START_WEEK).
          Judged on mean absolute error of the predicted margin for weeks 6+ and for the last 4 weeks, plus ATS%.
Part 2 - market blend: spread = market + k * (gamma - market) for k in 0..1 (MAE only; a blend never changes a pick's side,
          it shows how much weight Gamma deserves).

Ship a late weight only if it improves MAE for weeks 6+ by about 0.1 point or more. One season is a small sample.

Usage:
    python src/diagnose_late_weight.py
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
CONFIGS = [("current (flat 0.10)", None, 99)]
CONFIGS += [(f"{w:.2f} from week {s}", w, s) for s in (5, 8) for w in (0.15, 0.20, 0.25)]


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
    games = games[games["market_spread_home"].notna()]
    weeks = sorted(w for w in games["week"].unique() if w >= 2)
    print(f"{len(games)} games with lines, weeks {weeks[0]}-{weeks[-1]}.")

    saved = (gamma_model.LATE_PERFORMANCE_WEIGHT, gamma_model.LATE_START_WEEK)
    out, blend_rows = [], []
    try:
        for label, w, s in CONFIGS:
            gamma_model.LATE_PERFORMANCE_WEIGHT, gamma_model.LATE_START_WEEK = w, s
            rows = []
            for wk in weeks:
                ratings, _ = gamma_model.replay_ratings(con, season=SEASON, through_week=int(wk) - 1)
                for r in games[games["week"] == wk].itertuples():
                    h, a = normalize_team_name(r.home_team), normalize_team_name(r.away_team)
                    if h not in ratings or a not in ratings:
                        continue
                    spread = gamma_model.predict_spread_home(ratings, h, a, neutral_site=bool(r.neutral_site))
                    margin = r.home_points - r.away_points
                    res = backtest.grade_spread_pick(float(spread), float(r.market_spread_home), None, float(margin),
                                                     backtest.EDGE_THRESHOLD)["bet_result"]
                    rows.append({"week": wk, "err": abs(-spread - margin), "mkt_err": abs(-r.market_spread_home - margin),
                                 "spread": spread, "mkt": r.market_spread_home, "margin": margin, "res": res})
            df = pd.DataFrame(rows)

            def mae(d):
                return round(d["err"].mean(), 2) if len(d) else np.nan

            def ats(d):
                dec = d[d["res"].isin(["Win", "Loss"])]
                return round(100 * (dec["res"] == "Win").mean(), 1) if len(dec) else np.nan
            last4 = df[df["week"] >= max(weeks) - 3]
            out.append({"setting": label, "MAE_wk2+": mae(df), "MAE_wk6+": mae(df[df["week"] >= 6]),
                        "MAE_last4wks": mae(last4), "ATS%_wk6+": ats(df[df["week"] >= 6]), "market_MAE_wk6+": round(df[df["week"] >= 6]["mkt_err"].mean(), 2)})
            if label.startswith("current"):
                for k in (0, 0.25, 0.5, 0.75, 1.0):
                    s2 = df["mkt"] + k * (df["spread"] - df["mkt"])
                    blend_rows.append({"weight on Gamma (k)": k, "MAE_all": round((-s2 - df["margin"]).abs().mean(), 2),
                                       "MAE_wk6+": round((-s2 - df["margin"])[df["week"] >= 6].abs().mean(), 2)})
            print(f"  done: {label}", flush=True)
    finally:
        gamma_model.LATE_PERFORMANCE_WEIGHT, gamma_model.LATE_START_WEEK = saved

    print("\nPART 1 - late-season update weight\n" + pd.DataFrame(out).to_string(index=False))
    print("\nPART 2 - blend with the market (k=0 is the Vegas line, k=1 is pure Gamma)\n" + pd.DataFrame(blend_rows).to_string(index=False))


if __name__ == "__main__":
    main()
