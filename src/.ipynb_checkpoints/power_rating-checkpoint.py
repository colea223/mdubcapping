"""
Baseline Massey/Elo-style power rating (attack plan, Section 5, Step 1).

Deliberately simple and transparent: a 538-style Elo with a margin-of-victory
multiplier and a home-field bonus, recomputed from every completed FBS game in
the `games` table (all FBS, not just Mountain West -- so UTEP/Northern
Illinois bring their C-USA/MAC history and North Dakota State brings whatever
FCS-era games we pulled, per the realignment problem in the attack plan).

Walk-forward safety is the whole point of this file: for every game, we record
BOTH rating_before (what the team's rating was walking in -- safe to use as a
feature for that game) and rating_after (post-game update). Nothing here ever
uses a game's own result to describe that same game's pre-game state.

Known limitation: North Dakota State's FCS-era opponents mostly won't appear
in this FBS-only ratings run, so their ratings (and by extension NDSU's, until
it plays a few 2026 FBS games) default to the baseline and should be treated
as noisier than a normal FBS team's -- lean on the recruiting/talent prior for
it early in the season rather than trusting the Elo number alone.

Usage:
    source .venv/bin/activate
    python src/power_rating.py
"""
import math
from datetime import datetime

import duckdb

from config import DB_PATH

BASE_RATING = 1500.0
K_FACTOR = 20.0
HOME_FIELD_ADV = 65.0          # Elo points added to the home team's rating for win-prob purposes only
SEASON_REGRESSION = 0.75        # fraction of last rating carried into a new season (rest regresses to mean)


def expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10 ** (-(rating_a - rating_b) / 400.0))


def mov_multiplier(margin: int, elo_diff: float) -> float:
    # 538-style margin-of-victory multiplier: blowouts move the rating more,
    # but less so when the favorite was already expected to win big.
    return math.log(max(margin, 1) + 1) * (2.2 / (0.001 * abs(elo_diff) + 2.2))


def load_games(con):
    df = con.execute("""
        SELECT game_id, season, week, start_date, neutral_site,
               home_team, home_points, away_team, away_points
        FROM games
        WHERE completed = TRUE AND home_points IS NOT NULL AND away_points IS NOT NULL
        ORDER BY start_date, game_id
    """).fetchdf()
    return df


def run_ratings(con):
    games = load_games(con)
    if games.empty:
        print("power_rating: no completed games in the DB yet (run the pull scripts + build_db.py first)")
        return []

    rating = {}          # team -> current rating
    last_season = {}      # team -> season they last played, for the between-season regression
    rows = []

    for _, g in games.iterrows():
        home, away, season = g["home_team"], g["away_team"], g["season"]

        for team in (home, away):
            if team not in rating:
                rating[team] = BASE_RATING
                last_season[team] = season
            elif last_season[team] < season:
                rating[team] = BASE_RATING + SEASON_REGRESSION * (rating[team] - BASE_RATING)
                last_season[team] = season

        home_before, away_before = rating[home], rating[away]
        home_eff = home_before + (0.0 if g["neutral_site"] else HOME_FIELD_ADV)

        expected_home = expected_score(home_eff, away_before)
        margin = abs(int(g["home_points"]) - int(g["away_points"]))
        if g["home_points"] > g["away_points"]:
            actual_home = 1.0
        elif g["home_points"] < g["away_points"]:
            actual_home = 0.0
        else:
            actual_home = 0.5

        mult = mov_multiplier(margin, home_eff - away_before)
        delta = K_FACTOR * mult * (actual_home - expected_home)

        home_after = home_before + delta
        away_after = away_before - delta

        rating[home], rating[away] = home_after, away_after

        rows.append((g["game_id"], home, True, home_before, home_after))
        rows.append((g["game_id"], away, False, away_before, away_after))

    return rows


def write_ratings(con, rows):
    con.execute("DELETE FROM ratings_baseline")
    con.executemany("INSERT OR REPLACE INTO ratings_baseline VALUES (?,?,?,?,?)", rows)
    print(f"ratings_baseline: {len(rows)} rows ({len(rows) // 2} games)")


def current_ratings(con):
    """Latest rating_after per team -- for projecting games that haven't been played yet."""
    return dict(con.execute("""
        SELECT team, rating_after
        FROM ratings_baseline r
        JOIN games g ON g.game_id = r.game_id
        QUALIFY ROW_NUMBER() OVER (PARTITION BY team ORDER BY g.start_date DESC) = 1
    """).fetchall())


def main():
    con = duckdb.connect(str(DB_PATH))
    rows = run_ratings(con)
    if rows:
        write_ratings(con, rows)
        ranked = sorted(current_ratings(con).items(), key=lambda kv: -kv[1])
        print("\nTop 10 current ratings:")
        for team, r in ranked[:10]:
            print(f"  {r:7.1f}  {team}")
    con.close()


if __name__ == "__main__":
    main()