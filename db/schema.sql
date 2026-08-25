-- DuckDB schema: single source of truth for the MW handicapping pipeline.
-- Populated by src/build_db.py from the raw JSON snapshots in data/raw/.

CREATE TABLE IF NOT EXISTS teams (
    team            VARCHAR PRIMARY KEY,   -- canonical name, see src/teams.py
    prior_conference VARCHAR,
    joined_2026     BOOLEAN,
    notes           VARCHAR
);

CREATE TABLE IF NOT EXISTS games (
    game_id             BIGINT PRIMARY KEY,
    season              INTEGER,
    week                INTEGER,
    season_type         VARCHAR,
    start_date          TIMESTAMP,
    completed           BOOLEAN,
    neutral_site        BOOLEAN,
    conference_game     BOOLEAN,
    venue               VARCHAR,
    venue_id            BIGINT,
    home_team           VARCHAR,
    home_conference     VARCHAR,
    home_points         INTEGER,
    home_pregame_elo    INTEGER,
    away_team           VARCHAR,
    away_conference     VARCHAR,
    away_points         INTEGER,
    away_pregame_elo    INTEGER,
    excitement_index    DOUBLE
);

CREATE TABLE IF NOT EXISTS advanced_stats (
    season          INTEGER,
    team            VARCHAR,
    conference      VARCHAR,
    off_ppa         DOUBLE,
    off_success_rate DOUBLE,
    off_explosiveness DOUBLE,
    def_ppa         DOUBLE,
    def_success_rate DOUBLE,
    def_explosiveness DOUBLE,
    PRIMARY KEY (season, team)
);

CREATE TABLE IF NOT EXISTS sp_ratings (
    season          INTEGER,
    team            VARCHAR,
    conference      VARCHAR,
    rating          DOUBLE,
    ranking         INTEGER,
    offense_rating  DOUBLE,
    defense_rating  DOUBLE,
    special_teams_rating DOUBLE,
    PRIMARY KEY (season, team)
);

CREATE TABLE IF NOT EXISTS elo_ratings (
    season  INTEGER,
    team    VARCHAR,
    conference VARCHAR,
    elo     DOUBLE,
    PRIMARY KEY (season, team)
);

CREATE TABLE IF NOT EXISTS recruiting (
    season  INTEGER,
    team     VARCHAR,
    rank     INTEGER,
    points   DOUBLE,
    PRIMARY KEY (season, team)
);

-- One row per game per sportsbook provider (a game can have several).
CREATE TABLE IF NOT EXISTS lines (
    game_id         BIGINT,
    season          INTEGER,
    week            INTEGER,
    home_team       VARCHAR,
    away_team       VARCHAR,
    provider        VARCHAR,
    spread          DOUBLE,
    spread_open     DOUBLE,
    over_under      DOUBLE,
    over_under_open DOUBLE,
    home_moneyline  DOUBLE,
    away_moneyline  DOUBLE,
    PRIMARY KEY (game_id, provider)
);

-- Static venue metadata (lat/long/elevation) -- see src/pull_venues.py.
CREATE TABLE IF NOT EXISTS venues (
    venue_id        BIGINT PRIMARY KEY,
    name            VARCHAR,
    city            VARCHAR,
    state           VARCHAR,
    latitude        DOUBLE,
    longitude       DOUBLE,
    elevation_ft    DOUBLE,
    dome            BOOLEAN,
    grass           BOOLEAN,
    timezone        VARCHAR
);

-- Phase 2: baseline Massey/Elo-style power rating, walk-forward safe.
-- rating_before is the team's rating going INTO this game -- never uses this
-- game's own result -- which is exactly what feature engineering and
-- backtesting are required to key off of. See src/power_rating.py.
CREATE TABLE IF NOT EXISTS ratings_baseline (
    game_id         BIGINT,
    team            VARCHAR,
    is_home         BOOLEAN,
    rating_before   DOUBLE,
    rating_after    DOUBLE,
    PRIMARY KEY (game_id, team)
);

-- Phase 2: one row per game of leakage-safe pre-game features. See src/features.py.
-- sp_diff/ppa_diff/talent_diff added Phase 3.5 (backtest revealed the model
-- was ignoring SP+/PPA/recruiting data that pull_stats.py already collects).
--
-- CREATE OR REPLACE (not IF NOT EXISTS) is deliberate here: this table is
-- always fully rebuilt from scratch by features.py's DELETE+INSERT every run
-- anyway, so nothing is lost by dropping it -- and it means a future column
-- change here (like this one) doesn't require deleting the whole .duckdb
-- file, just rerunning the pipeline.
CREATE OR REPLACE TABLE game_features (
    game_id                 BIGINT PRIMARY KEY,
    season                  INTEGER,
    week                     INTEGER,
    home_team               VARCHAR,
    away_team               VARCHAR,
    neutral_site            BOOLEAN,
    conference_game         BOOLEAN,
    home_rest_days          INTEGER,
    away_rest_days          INTEGER,
    travel_km_away          DOUBLE,
    venue_elevation_ft      DOUBLE,
    elevation_delta_away_ft DOUBLE,
    home_rating_before      DOUBLE,
    away_rating_before      DOUBLE,
    rating_diff             DOUBLE,
    sp_diff                 DOUBLE,  -- prior-season SP+ rating, home minus away (preseason prior -- see note below)
    ppa_diff                DOUBLE,  -- prior-season net PPA (off_ppa - def_ppa), home minus away
    talent_diff             DOUBLE   -- THIS season's recruiting composite, home minus away
);
