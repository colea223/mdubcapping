"""
Leakage-safe pre-game feature table (attack plan, Section 4: Feature Engineer).

Every feature here is computable from information available BEFORE kickoff:
  - rest days for each team (days since their last game in the dataset)
  - travel distance for the away team (and the home team too, on a neutral site)
    from each team's usual home venue to this game's venue
  - elevation delta the away team faces relative to their own home elevation
    (the Air Force / New Mexico / Wyoming altitude edge from the attack plan)
  - each team's power rating walking INTO this game:
      * for a game that's already been played, this is ratings_baseline's
        rating_before (computed by power_rating.py without using this game's
        own result)
      * for a game that hasn't been played yet, this is the team's current
        rating as of today (power_rating.current_ratings()) -- there's no
        rating_before yet because the Elo engine only updates on completed
        games, so "as of now" is the correct stand-in for a future matchup

Usage:
    source .venv/bin/activate
    python src/power_rating.py   # must run first -- features reads its output
    python src/features.py
"""
import math

import duckdb
import pandas as pd

from config import DB_PATH
from power_rating import current_ratings

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2):
    if any(v is None or pd.isna(v) for v in (lat1, lon1, lat2, lon2)):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def team_home_venues(games: pd.DataFrame) -> dict:
    """Each team's most common home venue -- used as the 'origin' for travel distance."""
    home_games = games[~games["neutral_site"]]
    mode = (
        home_games.groupby("home_team")["venue_id"]
        .agg(lambda s: s.mode().iat[0] if not s.mode().empty else None)
    )
    return mode.to_dict()


def compute_rest_days(games: pd.DataFrame) -> pd.DataFrame:
    """Long-format (game_id, team, is_home, rest_days), one row per team-appearance."""
    home = games[["game_id", "start_date", "home_team"]].rename(columns={"home_team": "team"})
    home["is_home"] = True
    away = games[["game_id", "start_date", "away_team"]].rename(columns={"away_team": "team"})
    away["is_home"] = False
    long = pd.concat([home, away], ignore_index=True).sort_values(["team", "start_date"])
    long["prev_date"] = long.groupby("team")["start_date"].shift(1)
    long["rest_days"] = (long["start_date"] - long["prev_date"]).dt.days
    return long[["game_id", "team", "is_home", "rest_days"]]


def build_features(con) -> pd.DataFrame:
    games = con.execute("""
        SELECT game_id, season, week, start_date, neutral_site, conference_game,
               venue_id, home_team, away_team
        FROM games
    """).fetchdf()
    if games.empty:
        return pd.DataFrame()

    venues = con.execute("SELECT venue_id, latitude, longitude, elevation_ft FROM venues").fetchdf()
    venues = venues.set_index("venue_id") if not venues.empty else venues

    ratings_hist = con.execute("SELECT game_id, team, rating_before FROM ratings_baseline").fetchdf()
    ratings_hist_map = {(r.game_id, r.team): r.rating_before for r in ratings_hist.itertuples()}
    ratings_now = current_ratings(con)

    home_venue = team_home_venues(games)
    rest = compute_rest_days(games)
    rest_map = {(r.game_id, r.team): r.rest_days for r in rest.itertuples()}

    def venue_row(vid):
        if venues is None or venues.empty or vid not in venues.index:
            return None, None, None
        row = venues.loc[vid]
        return row["latitude"], row["longitude"], row["elevation_ft"]

    def rating_for(game_id, team):
        if (game_id, team) in ratings_hist_map:
            return ratings_hist_map[(game_id, team)]
        return ratings_now.get(team)  # upcoming game -> latest known rating

    rows = []
    for g in games.itertuples():
        lat, lon, elev = venue_row(g.venue_id)

        away_home_vid = home_venue.get(g.away_team)
        away_lat, away_lon, away_elev = venue_row(away_home_vid) if away_home_vid else (None, None, None)
        travel_km_away = haversine_km(lat, lon, away_lat, away_lon)
        elevation_delta_away = (elev - away_elev) if (elev is not None and away_elev is not None) else None

        home_before = rating_for(g.game_id, g.home_team)
        away_before = rating_for(g.game_id, g.away_team)
        rating_diff = (home_before - away_before) if (home_before is not None and away_before is not None) else None

        rows.append((
            g.game_id, g.season, g.week, g.home_team, g.away_team,
            g.neutral_site, g.conference_game,
            rest_map.get((g.game_id, g.home_team)),
            rest_map.get((g.game_id, g.away_team)),
            travel_km_away, elev, elevation_delta_away,
            home_before, away_before, rating_diff,
        ))

    return pd.DataFrame(rows, columns=[
        "game_id", "season", "week", "home_team", "away_team",
        "neutral_site", "conference_game",
        "home_rest_days", "away_rest_days",
        "travel_km_away", "venue_elevation_ft", "elevation_delta_away_ft",
        "home_rating_before", "away_rating_before", "rating_diff",
    ])


def main():
    con = duckdb.connect(str(DB_PATH))
    df = build_features(con)
    if df.empty:
        print("features: no games in the DB yet (run the pull scripts + build_db.py first)")
        con.close()
        return

    con.execute("DELETE FROM game_features")
    con.register("df_features", df)
    con.execute("INSERT INTO game_features SELECT * FROM df_features")
    print(f"game_features: {len(df)} rows")
    con.close()


if __name__ == "__main__":
    main()