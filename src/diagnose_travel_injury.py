"""
Do travel distance and pre-game injuries deserve a place in Dub Gamma's
SPREAD (not just its rating update)?  READ-ONLY on the database.

WHERE THINGS STAND TODAY
  * Gamma's injury term only enters the rating UPDATE after a game
    (TGPL = net score + opponent rating + own_injury - opp_injury), so a team
    that wins short-handed gets extra credit afterwards. It is NOT applied to
    the spread of an upcoming game, so Gamma's line for BYU-with-the-RB-out
    equals its line for BYU-at-full-strength.
  * Gamma has no travel input at all (home field is the flat HFA constant).
    Ridge and XGBoost already include travel_km_away.

THIS SCRIPT PRINTS THREE THINGS
  1. Injury data coverage: how many weeks / teams / players were scraped, how
     many players matched to CFBD PPA, and the biggest current injury burdens.
  2. Travel evidence from 2016-2025 history: points of margin per 1,000 km of
     away-team travel after controlling for team strength (Elo diff), rest,
     and elevation, and -- the key question for betting -- whether the MARKET
     already prices it (a coefficient near zero on the cover margin means yes).
  3. A 2026 walk-forward test of Gamma's spread with a travel adjustment and/or
     a pre-game injury adjustment, over a small grid, graded with
     backtest.grade_spread_pick. Gamma only exists for 2026, so this sample is
     small: treat it as a sanity check, not proof.

Usage:
    python src/diagnose_travel_injury.py
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
TRAVEL_GRID = [0.0, 0.5, 1.0, 2.0]    # points per 1,000 km of away-team travel
INJURY_GRID = [0.0, 0.5, 1.0, 2.0]    # points per unit of injury-burden difference
INJURY_CAP = 7.0                      # cap on the injury adjustment, in points


def ols(X, y, names):
    """Plain OLS with standard errors. X: (n,k) array (no intercept column)."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    X = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = max(len(y) - X.shape[1], 1)
    sigma2 = resid @ resid / dof
    cov = sigma2 * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    return pd.DataFrame({"coef": beta, "se": se, "t": beta / se}, index=["intercept"] + list(names))


def evaluate(rows, travel_c, inj_k):
    """rows: list of dicts with base_spread, market, margin, travel_km, home_inj, away_inj.
    Spread convention: negative = home favored. Away travel hurts the away team
    (line moves toward home); an injury burden hurts the team that has it."""
    wins = losses = 0
    abs_err = []
    touched = 0
    for r in rows:
        adj = 0.0
        if travel_c and r["travel_km"] is not None and not np.isnan(r["travel_km"]):
            adj -= travel_c * r["travel_km"] / 1000.0
        if inj_k:
            inj = inj_k * (r["home_inj"] - r["away_inj"])
            adj += max(-INJURY_CAP, min(INJURY_CAP, inj))
        spread = r["base_spread"] + adj
        if adj:
            touched += 1
        g = backtest.grade_spread_pick(float(spread), float(r["market"]), None,
                                       float(r["margin"]), backtest.EDGE_THRESHOLD)
        if g["bet_result"] == "Win":
            wins += 1
        elif g["bet_result"] == "Loss":
            losses += 1
        abs_err.append(abs(-spread - r["margin"]))
    n = wins + losses
    return {"travel_c": travel_c, "inj_k": inj_k, "games": n,
            "ATS%": round(100 * wins / n, 1) if n else np.nan,
            "MAE_margin": round(float(np.mean(abs_err)), 2) if abs_err else np.nan,
            "games_adjusted": touched}


def section_injury_coverage(con):
    print("\n" + "=" * 70 + "\n1. INJURY DATA COVERAGE\n" + "=" * 70)
    try:
        cov = con.execute("""
            SELECT season, week, COUNT(*) AS player_rows, COUNT(DISTINCT team) AS teams
            FROM injury_reports GROUP BY season, week ORDER BY season, week
        """).fetchdf()
    except Exception as e:
        print(f"injury_reports unavailable: {str(e).splitlines()[0]}")
        return None
    print(cov.to_string(index=False))
    weights = gamma_model.load_injury_weights_by_week(con, SEASON)
    by_week = {}
    for (team, wk), w in weights.items():
        by_week.setdefault(wk, []).append((w, team))
    print(f"\nTeam-weeks with a non-zero injury burden in {SEASON} (matched to a player with positive PPA):")
    for wk in sorted(by_week):
        vals = [w for w, _ in by_week[wk]]
        top = sorted(by_week[wk], reverse=True)[:5]
        print(f"  week {wk}: {len(vals)} teams, median {np.median(vals):.2f}, max {max(vals):.2f}; "
              f"top: " + ", ".join(f"{t} {w:.1f}" for w, t in top))
    n_rows = con.execute("SELECT COUNT(*) FROM injury_reports WHERE season = ?", [SEASON]).fetchone()[0]
    matched = sum(len(v) for v in by_week.values())
    print(f"\n{n_rows} player rows scraped in {SEASON}; {matched} team-weeks ended up with a usable burden.")
    return weights


def section_travel_history(con):
    print("\n" + "=" * 70 + "\n2. TRAVEL EVIDENCE, 2016-2025\n" + "=" * 70)
    df = con.execute("""
        SELECT f.game_id, f.season, f.neutral_site, f.travel_km_away, f.rating_diff,
               f.home_rest_days, f.away_rest_days, f.elevation_delta_away_ft,
               g.home_points, g.away_points, m.market_spread_home
        FROM game_features f
        JOIN games g ON g.game_id = f.game_id
        LEFT JOIN (SELECT game_id, AVG(spread) AS market_spread_home FROM lines GROUP BY game_id) m
               ON m.game_id = f.game_id
        WHERE g.completed = TRUE AND g.season_type = 'regular' AND f.season <= 2025
    """).fetchdf()
    df = df[~df["neutral_site"].astype(bool)].copy()
    for c in ["travel_km_away", "rating_diff", "home_rest_days", "away_rest_days",
              "elevation_delta_away_ft", "home_points", "away_points", "market_spread_home"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["margin"] = df["home_points"] - df["away_points"]
    df["travel_1000km"] = df["travel_km_away"] / 1000.0
    df["rest_diff"] = df["home_rest_days"] - df["away_rest_days"]
    df["elev_1000ft"] = df["elevation_delta_away_ft"] / 1000.0
    cols = ["travel_1000km", "rating_diff", "rest_diff", "elev_1000ft"]

    d1 = df.dropna(subset=cols + ["margin"])
    print(f"\nA) Margin ~ strength + travel + rest + elevation  ({len(d1)} non-neutral games)")
    print(ols(d1[cols].values, d1["margin"].values, cols).round(3).to_string())
    print("   -> travel_1000km coef = points of home margin per 1,000 km the away team travels")

    d2 = df.dropna(subset=["travel_1000km", "rest_diff", "elev_1000ft", "market_spread_home", "margin"]).copy()
    d2["home_cover_margin"] = d2["margin"] + d2["market_spread_home"]   # >0 means home covered
    cols2 = ["travel_1000km", "rest_diff", "elev_1000ft"]
    print(f"\nB) Does the MARKET already price it?  Home cover margin ~ travel + rest + elevation  ({len(d2)} games)")
    print(ols(d2[cols2].values, d2["home_cover_margin"].values, cols2).round(3).to_string())
    print("   -> if travel_1000km is near 0 (|t| < 2), the market already prices travel; adding it to")
    print("      Gamma would only move Gamma toward the market, not create an edge.")


def section_gamma_walkforward(con, weights):
    print("\n" + "=" * 70 + f"\n3. GAMMA {SEASON} WALK-FORWARD: travel / injury adjustment grid\n" + "=" * 70)
    games = con.execute("""
        SELECT g.game_id, g.week, g.home_team, g.away_team, g.home_points, g.away_points,
               g.neutral_site, f.travel_km_away, m.market_spread_home
        FROM games g
        LEFT JOIN game_features f ON f.game_id = g.game_id
        LEFT JOIN (SELECT game_id, AVG(spread) AS market_spread_home FROM lines GROUP BY game_id) m
               ON m.game_id = g.game_id
        WHERE g.season = ? AND g.season_type = 'regular' AND g.completed = TRUE
          AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
        ORDER BY g.week
    """, [SEASON]).fetchdf()
    games = games[games["market_spread_home"].notna()]
    weights = weights or {}
    rows = []
    for wk, grp in games.groupby("week"):
        if wk < 2:
            continue
        ratings, _ = gamma_model.replay_ratings(con, season=SEASON, through_week=int(wk) - 1)
        ratings = dict(ratings)   # plain dict: strips Gamma's built-in adjustment so the grid starts from the raw line
        for r in grp.itertuples():
            h, a = normalize_team_name(r.home_team), normalize_team_name(r.away_team)
            if h not in ratings or a not in ratings:
                continue
            base = gamma_model.predict_spread_home(ratings, h, a, neutral_site=bool(r.neutral_site))
            rows.append({
                "base_spread": base, "market": r.market_spread_home,
                "margin": r.home_points - r.away_points,
                "travel_km": None if bool(r.neutral_site) else r.travel_km_away,
                "home_inj": weights.get((h, wk), weights.get((r.home_team, wk), 0.0)),
                "away_inj": weights.get((a, wk), weights.get((r.away_team, wk), 0.0)),
            })
    print(f"{len(rows)} graded games (weeks 2+). Note: Gamma exists only for {SEASON}, so this is a small sample --")
    print("a +/-1.5 pt ATS swing is within luck at this size.\n")
    if not rows:
        print("No games to grade.")
        return
    res = [evaluate(rows, 0.0, 0.0)]
    res += [evaluate(rows, c, 0.0) for c in TRAVEL_GRID if c]
    res += [evaluate(rows, 0.0, k) for k in INJURY_GRID if k]
    res += [evaluate(rows, c, k) for c in TRAVEL_GRID if c for k in INJURY_GRID if k]
    print(pd.DataFrame(res).to_string(index=False))
    base = res[0]
    print(f"\nBaseline (no adjustments): {base['ATS%']}% ATS, MAE {base['MAE_margin']}.")
    print("Look for a setting that improves BOTH ATS% and MAE; prefer small coefficients that sit in a")
    print("smooth region of the grid over one lucky cell.")


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    weights = section_injury_coverage(con)
    section_travel_history(con)
    section_gamma_walkforward(con, weights)


if __name__ == "__main__":
    main()
