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
  - sp_diff / ppa_diff: SP+ and PPA are SEASON-END aggregates in CFBD (one
    number computed from that whole season's games) -- using a team's OWN
    season's SP+/PPA as a feature for a game IN that season would leak the
    rest of the season into the prediction. These two instead use the PRIOR
    season's final SP+/net-PPA as a preseason-style prior, which is fully
    known before a single game of the current season is played.
  - talent_diff: recruiting classes are set (signing day) before the season
    starts, so THIS season's recruiting composite is safe to use as-is.
  - drive_yards_diff / drive_points_diff / drive_turnovers_diff /
    pass_ypd_diff / rush_ypd_diff / ypa_diff / ypc_diff: drive-based rate
    stats (see db/schema.sql's drive_stats_snapshots comment and
    src/pull_drives.py / src/pull_plays.py). Same prior-season-only
    treatment as sp_diff/ppa_diff, and for the same leakage reason -- these
    numbers are a full SEASON's aggregate, so using a team's OWN current
    season would leak the rest of that season into the prediction. Looked
    up as the row with the LAST as_of_week of season - 1 for each team,
    which is exactly that whole prior season's rate (drive_stats_snapshots
    also supports an in-season, walk-forward-safe version via as_of_week <=
    W - 1 within the CURRENT season, but that's deliberately NOT used here
    -- see totals_model.py's docstring for why a raw in-season, publicly-
    observable stat previously made this project's totals model WORSE, not
    better, and drive rate stats are just as public/fast-moving. Wiring the
    in-season version in would need its own backtest confirming it helps
    first.)
  - std_down_ppa_diff / passing_down_ppa_diff / red_zone_ppa_diff /
    explosive_rate_diff: down-and-distance situational splits (see
    db/schema.sql's situational_stats_snapshots comment and
    src/build_db.py's build_situational_stats_snapshots_table() for the
    standard-down/passing-down/red-zone/explosive definitions). Same
    prior-season-only, last-as_of_week lookup as the drive-based features
    above, and for the same leakage reason. Each team's own situational
    rating nets its offense against its defense first (off_X minus
    def_X -- the same off_ppa-minus-def_ppa framing ppa_map already uses
    for the overall, non-situational PPA diff), THEN the diff is that net
    rating, home minus away.

Usage:
    source .venv/bin/activate
    python src/power_rating.py   # must run first -- features reads its output
    python src/features.py
"""
import math

import duckdb
import pandas as pd
import time

from config import DB_PATH
from power_rating import current_ratings
from teams import FBS_CONFERENCES

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

    # Season-by-season FBS/FCS check, keyed (season, team) -- see teams.py's
    # own FBS_CONFERENCES comment for the full North Dakota State/FCS-
    # contamination story. advanced_stats (ppa_map below) doesn't need this:
    # pull_stats.py's get_advanced_season_stats() call relies on CFBD's own
    # classification parameter, which DEFAULTS to 'fbs', so a team's FCS-era
    # rows (e.g. North Dakota State's every pre-2026 season) never even make
    # it into that table. get_sp() and get_team_recruiting_rankings() (behind
    # sp_map/talent_map) have NO classification parameter at all, though --
    # pull_stats.py pulls every team CFBD has a rating/recruiting class for
    # that year, FCS included -- so without this check, a newly-promoted
    # team's FCS-era SP+ rating or FCS-scale recruiting composite would get
    # used as if it were on the same footing as an FBS team's, exactly the
    # same contamination mechanism that hit situational_stats_snapshots (see
    # src/diagnose_situational_features.py and that table's own fix).
    conf_hist = con.execute("""
        SELECT season, home_team AS team, home_conference AS conf FROM games
        UNION
        SELECT season, away_team AS team, away_conference AS conf FROM games
    """).fetchdf()
    conf_hist_map = {(r.season, r.team): r.conf for r in conf_hist.itertuples()}

    def _is_fbs(season, team):
        return conf_hist_map.get((season, team)) in FBS_CONFERENCES

    # Prior-season SP+ and net PPA (off_ppa - def_ppa), keyed by (season, team)
    # so a game in `season` looks up `season - 1`'s value -- see the leakage
    # note above. Recruiting talent uses the SAME season (safe -- set before
    # the season starts), keyed the same way for a consistent lookup pattern.
    sp_map = {
        (r.season, r.team): r.rating
        for r in con.execute("SELECT season, team, rating FROM sp_ratings").fetchdf().itertuples()
        if _is_fbs(r.season, r.team)
    }
    ppa_map = {
        (r.season, r.team): r.off_ppa - r.def_ppa
        for r in con.execute("SELECT season, team, off_ppa, def_ppa FROM advanced_stats").fetchdf().itertuples()
        if pd.notna(r.off_ppa) and pd.notna(r.def_ppa)
    }
    talent_map = {
        (r.season, r.team): r.points
        for r in con.execute("SELECT season, team, points FROM recruiting").fetchdf().itertuples()
        if _is_fbs(r.season, r.team)
    }

    # Prior-season drive-based rate stats -- the LAST as_of_week row for
    # each (season, team) in drive_stats_snapshots IS that whole season's
    # final rate (see the leakage note above and schema.sql's comment).
    # Wrapped defensively: an older database that hasn't had build_db.py
    # rerun since this table was added would otherwise crash features.py
    # outright rather than just running without these features.
    try:
        drive_stats_df = con.execute("""
            SELECT ds.season, ds.team, ds.yards_per_drive, ds.points_per_drive,
                   ds.turnovers_per_drive, ds.pass_yards_per_drive, ds.rush_yards_per_drive,
                   ds.yards_per_attempt, ds.yards_per_carry
            FROM drive_stats_snapshots ds
            INNER JOIN (
                SELECT season, team, MAX(as_of_week) AS max_week
                FROM drive_stats_snapshots GROUP BY season, team
            ) mx ON mx.season = ds.season AND mx.team = ds.team AND mx.max_week = ds.as_of_week
        """).fetchdf()
        drive_map = {(r.season, r.team): r for r in drive_stats_df.itertuples()}
    except duckdb.Error:
        print("features: drive_stats_snapshots table not found -- drive-based diff features will "
              "be all-NULL. Rerun src/build_db.py (which creates it from schema.sql) to fix this.")
        drive_map = {}

    # Prior-season situational (down/distance) rate stats -- same
    # last-as_of_week-of-prior-season lookup as drive_map above, and the
    # same defensive try/except so an older database that hasn't had
    # build_db.py rerun since this table was added just runs without these
    # features instead of crashing.
    try:
        situational_df = con.execute("""
            SELECT ss.season, ss.team, ss.off_std_down_ppa, ss.def_std_down_ppa,
                   ss.off_passing_down_ppa, ss.def_passing_down_ppa,
                   ss.off_red_zone_ppa, ss.def_red_zone_ppa,
                   ss.off_explosive_rate, ss.def_explosive_rate
            FROM situational_stats_snapshots ss
            INNER JOIN (
                SELECT season, team, MAX(as_of_week) AS max_week
                FROM situational_stats_snapshots GROUP BY season, team
            ) mx ON mx.season = ss.season AND mx.team = ss.team AND mx.max_week = ss.as_of_week
        """).fetchdf()
        situational_map = {(r.season, r.team): r for r in situational_df.itertuples()}
    except duckdb.Error:
        print("features: situational_stats_snapshots table not found -- down/distance diff "
              "features will be all-NULL. Rerun src/build_db.py (which creates it from "
              "schema.sql) to fix this.")
        situational_map = {}

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

        prior_season = g.season - 1
        home_sp, away_sp = sp_map.get((prior_season, g.home_team)), sp_map.get((prior_season, g.away_team))
        sp_diff = (home_sp - away_sp) if (home_sp is not None and away_sp is not None) else None

        home_ppa, away_ppa = ppa_map.get((prior_season, g.home_team)), ppa_map.get((prior_season, g.away_team))
        ppa_diff = (home_ppa - away_ppa) if (home_ppa is not None and away_ppa is not None) else None

        home_talent, away_talent = talent_map.get((g.season, g.home_team)), talent_map.get((g.season, g.away_team))
        talent_diff = (home_talent - away_talent) if (home_talent is not None and away_talent is not None) else None

        home_drive, away_drive = drive_map.get((prior_season, g.home_team)), drive_map.get((prior_season, g.away_team))

        def _drive_diff(field):
            if home_drive is None or away_drive is None:
                return None
            h, a = getattr(home_drive, field), getattr(away_drive, field)
            if h is None or a is None or pd.isna(h) or pd.isna(a):
                return None
            return h - a

        drive_yards_diff = _drive_diff("yards_per_drive")
        drive_points_diff = _drive_diff("points_per_drive")
        drive_turnovers_diff = _drive_diff("turnovers_per_drive")
        pass_ypd_diff = _drive_diff("pass_yards_per_drive")
        rush_ypd_diff = _drive_diff("rush_yards_per_drive")
        ypa_diff = _drive_diff("yards_per_attempt")
        ypc_diff = _drive_diff("yards_per_carry")

        home_situational = situational_map.get((prior_season, g.home_team))
        away_situational = situational_map.get((prior_season, g.away_team))

        def _situational_diff(off_field, def_field):
            if home_situational is None or away_situational is None:
                return None
            h_off, h_def = getattr(home_situational, off_field), getattr(home_situational, def_field)
            a_off, a_def = getattr(away_situational, off_field), getattr(away_situational, def_field)
            if any(v is None or pd.isna(v) for v in (h_off, h_def, a_off, a_def)):
                return None
            return (h_off - h_def) - (a_off - a_def)

        std_down_ppa_diff = _situational_diff("off_std_down_ppa", "def_std_down_ppa")
        passing_down_ppa_diff = _situational_diff("off_passing_down_ppa", "def_passing_down_ppa")
        red_zone_ppa_diff = _situational_diff("off_red_zone_ppa", "def_red_zone_ppa")
        explosive_rate_diff = _situational_diff("off_explosive_rate", "def_explosive_rate")

        rows.append((
            g.game_id, g.season, g.week, g.home_team, g.away_team,
            g.neutral_site, g.conference_game,
            rest_map.get((g.game_id, g.home_team)),
            rest_map.get((g.game_id, g.away_team)),
            travel_km_away, elev, elevation_delta_away,
            home_before, away_before, rating_diff,
            sp_diff, ppa_diff, talent_diff,
            drive_yards_diff, drive_points_diff, drive_turnovers_diff,
            pass_ypd_diff, rush_ypd_diff, ypa_diff, ypc_diff,
            std_down_ppa_diff, passing_down_ppa_diff, red_zone_ppa_diff, explosive_rate_diff,
        ))

    return pd.DataFrame(rows, columns=[
        "game_id", "season", "week", "home_team", "away_team",
        "neutral_site", "conference_game",
        "home_rest_days", "away_rest_days",
        "travel_km_away", "venue_elevation_ft", "elevation_delta_away_ft",
        "home_rating_before", "away_rating_before", "rating_diff",
        "sp_diff", "ppa_diff", "talent_diff",
        "drive_yards_diff", "drive_points_diff", "drive_turnovers_diff",
        "pass_ypd_diff", "rush_ypd_diff", "ypa_diff", "ypc_diff",
        "std_down_ppa_diff", "passing_down_ppa_diff", "red_zone_ppa_diff", "explosive_rate_diff",
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
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
