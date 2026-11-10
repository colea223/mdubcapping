"""
Schedule-spot test: does the betting market under- or over-price scheduling situations?  READ-ONLY on the database.

Situations tested (all computed from the games table, using only a team's EARLIER games in the same season):
  road2   this is the team's 2nd straight true road game          road3   3rd (or more) straight road game
  bye     coming off 12+ days of rest (bye week)                 short   5 or fewer days since last game (Thu/Fri after a Sat)
  letdown previous game was a 21+ point WIN                      hangover previous game was a 21+ point LOSS
  rivalry a hand-made list of well-known rivalry games (RIVALRIES below -- check it, it is from memory, not a data source);
          tested as "does the UNDERDOG cover more often in rivalry games?"

Method: market miss r = actual home margin - market home margin = margin + market_spread_home (positive = the home side
covered). Regress r on (home flag - away flag) for each team situation, plus the rivalry term, over 2016-2025 regular-season
games with a closing line. A coefficient is "points the market is off per team in that spot" -- if it is near zero, the
market already prices the spot and adding it to Gamma would only move Gamma toward the market, not create an edge.
t-stats use robust (HC1) standard errors. Results are also split 2016-2020 vs 2021-2025: a real effect should have the same
sign in both halves. 2026 is shown separately (small sample).

Usage:
    python src/diagnose_schedule_spots.py
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DB_PATH  # noqa: E402

RIVALRIES = [
    ("Ohio State", "Michigan"), ("Alabama", "Auburn"), ("Texas", "Oklahoma"), ("Georgia", "Florida"),
    ("Georgia", "Georgia Tech"), ("Florida", "Florida State"), ("Clemson", "South Carolina"), ("Notre Dame", "USC"),
    ("Oregon", "Oregon State"), ("Washington", "Washington State"), ("Utah", "BYU"), ("Army", "Navy"),
    ("Michigan", "Michigan State"), ("Penn State", "Ohio State"), ("Michigan", "Notre Dame"), ("Texas", "Texas A&M"),
    ("Oklahoma", "Oklahoma State"), ("Alabama", "Tennessee"), ("LSU", "Alabama"), ("LSU", "Arkansas"),
    ("Ole Miss", "Mississippi State"), ("Kentucky", "Louisville"), ("Virginia", "Virginia Tech"),
    ("North Carolina", "NC State"), ("North Carolina", "Duke"), ("Miami", "Florida State"), ("Miami", "Florida"),
    ("Stanford", "California"), ("UCLA", "USC"), ("Arizona", "Arizona State"), ("Colorado", "Utah"),
    ("Iowa", "Iowa State"), ("Iowa", "Minnesota"), ("Wisconsin", "Minnesota"), ("Nebraska", "Iowa"),
    ("Purdue", "Indiana"), ("Illinois", "Northwestern"), ("Pittsburgh", "West Virginia"), ("Kansas", "Kansas State"),
    ("Missouri", "Kansas"), ("Cincinnati", "Louisville"), ("TCU", "Baylor"), ("TCU", "SMU"), ("Houston", "Rice"),
    ("Tulsa", "Tulane"), ("Boise State", "Utah State"), ("Colorado State", "Wyoming"), ("Air Force", "Army"),
    ("Air Force", "Navy"), ("UNLV", "Nevada"), ("New Mexico", "New Mexico State"), ("Fresno State", "San Diego State"),
    ("San Jose State", "Fresno State"), ("Hawaii", "San Diego State"), ("Boston College", "Syracuse"),
    ("Cal", "Oregon"), ("Appalachian State", "Georgia Southern"), ("Troy", "South Alabama"),
    ("Ohio", "Miami (OH)"), ("Toledo", "Bowling Green"), ("Western Michigan", "Central Michigan"),
]
RIVAL_SET = {frozenset(p) for p in RIVALRIES}
SPOTS = ["road2", "road3", "bye", "short", "letdown", "hangover"]


def team_spots(games):
    """One row per (game_id, team): flags for that team's situation going into the game."""
    g = games.sort_values("start_date")
    rows = []
    for (season, team), grp in pd.concat([
        g.assign(team=g["home_team"], is_home=True, own=g["home_points"], opp=g["away_points"]),
        g.assign(team=g["away_team"], is_home=False, own=g["away_points"], opp=g["home_points"]),
    ]).groupby(["season", "team"]):
        grp = grp.sort_values("start_date")
        prev_date, prev_margin, streak = None, None, 0
        for r in grp.itertuples():
            road = (not r.is_home) and (not bool(r.neutral_site))
            rest = (r.start_date - prev_date).days if prev_date is not None else None
            this_streak = (streak + 1) if road else 0
            rows.append({
                "game_id": r.game_id, "team": r.team, "is_home": r.is_home,
                "road2": int(road and streak >= 1), "road3": int(road and streak >= 2),
                "bye": int(rest is not None and rest >= 12), "short": int(rest is not None and rest <= 5),
                "letdown": int(prev_margin is not None and prev_margin >= 21),
                "hangover": int(prev_margin is not None and prev_margin <= -21),
            })
            streak = this_streak if road else 0
            prev_date = r.start_date
            prev_margin = (r.own - r.opp) if pd.notna(r.own) and pd.notna(r.opp) else None
    return pd.DataFrame(rows)


def ols(X, y, names):
    X = np.column_stack([np.ones(len(X)), X]).astype(float)
    y = np.asarray(y, dtype=float)
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    resid = y - X @ beta
    XtXi = np.linalg.pinv(X.T @ X)
    meat = X.T @ (X * (resid ** 2)[:, None])
    cov = XtXi @ meat @ XtXi * len(y) / max(len(y) - X.shape[1], 1)
    se = np.sqrt(np.diag(cov))
    return pd.DataFrame({"coef": beta, "se": se, "t": beta / se}, index=["intercept"] + names)


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    games = con.execute("""
        SELECT g.game_id, g.season, g.week, g.start_date, g.home_team, g.away_team, g.neutral_site,
               g.home_points, g.away_points, g.season_type
        FROM games g WHERE g.completed = TRUE AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
    """).fetchdf()
    lines = con.execute("SELECT game_id, AVG(spread) AS market FROM lines WHERE spread IS NOT NULL GROUP BY game_id").fetchdf()
    spots = team_spots(games)
    home = spots[spots.is_home].set_index("game_id")[SPOTS].add_prefix("h_")
    away = spots[~spots.is_home].set_index("game_id")[SPOTS].add_prefix("a_")
    df = games.merge(lines, on="game_id").set_index("game_id").join(home).join(away).dropna(subset=["market"])
    df = df[(df.season_type == "regular")].copy()
    df["r"] = (df.home_points - df.away_points) + df["market"]
    for s in SPOTS:
        df[s] = df[f"h_{s}"].fillna(0) - df[f"a_{s}"].fillna(0)
    df["rival"] = [frozenset((h, a)) in RIVAL_SET for h, a in zip(df.home_team, df.away_team)]
    dog_home = np.where(df["market"] > 0, 1, np.where(df["market"] < 0, -1, 0))        # home is the underdog
    df["rival_dog"] = df["rival"].astype(int) * dog_home
    names = SPOTS + ["rival_dog"]

    def run(label, d):
        res = ols(d[names].values, d["r"].values, names)
        print(f"\n{label}: {len(d)} games")
        res["n_flagged"] = [len(d)] + [int((d[n] != 0).sum()) for n in names]
        print(res.round(2).to_string())

    hist = df[df.season <= 2025]
    run("2016-2025 (all)", hist)
    run("  first half 2016-2020", hist[hist.season <= 2020])
    run("  second half 2021-2025", hist[hist.season >= 2021])
    run("2026 so far (small sample)", df[df.season == 2026])
    print(f"\nRivalry games in sample: {int(hist['rival'].sum())} of {len(hist)}.")
    print("coef = points the home side covers by, per unit of (home spot - away spot); rival_dog > 0 means the underdog beats the line in rivalry games.")
    print("Treat |t| < 2 as 'the market already prices it', and ignore any effect whose sign flips between the two halves.")


if __name__ == "__main__":
    main()
