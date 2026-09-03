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
    off_plays       INTEGER,  -- season-total offensive snaps -- see totals_model.py's pace feature (plays/game = off_plays / games played that season)
    def_ppa         DOUBLE,
    def_success_rate DOUBLE,
    def_explosiveness DOUBLE,
    PRIMARY KEY (season, team)
);

-- In-season, walk-forward-safe efficiency snapshots -- see totals_model.py's
-- in-season PPA feature and src/backfill_ppa_snapshots.py / the extra step
-- at the bottom of src/pull_stats.py. Unlike advanced_stats above (one row
-- per team per FULL season, only ever usable as a PRIOR-season signal),
-- this has one row per (season, team, as_of_week): CFBD's advanced-stats
-- aggregation computed only through games up to and including as_of_week,
-- via StatsApi.get_advanced_season_stats(year=season, end_week=as_of_week).
-- That week cutoff is what makes an IN-season signal possible without
-- leaking future games -- a team's row for as_of_week=9 reflects weeks 1-9
-- only, nothing later. totals_model.py's ASOF join only ever matches a game
-- in week W to a row with as_of_week <= W - 1, so even a game itself in
-- week W can't see that week's own numbers.
--
-- CFBD's SP+ ratings endpoint (RatingsApi.get_sp) has no equivalent week
-- parameter -- only `year`, returning one full-season number -- so this
-- same treatment can't be done for SP+ without snapshotting it forward from
-- today with no ability to backfill history to validate against first. See
-- the in-season PPA vs in-season SP+ discussion in totals_model.py's
-- docstring for why these were split into two separate candidates instead
-- of one, and why PPA went first.
CREATE TABLE IF NOT EXISTS ppa_snapshots (
    season              INTEGER,
    team                VARCHAR,
    as_of_week          INTEGER,
    off_ppa             DOUBLE,
    off_success_rate    DOUBLE,
    off_explosiveness   DOUBLE,
    def_ppa             DOUBLE,
    def_success_rate    DOUBLE,
    def_explosiveness   DOUBLE,
    PRIMARY KEY (season, team, as_of_week)
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

-- Every historical pull of pull_lines.py, kept forever (never overwritten),
-- one row per (game, provider, pulled_at) -- this is what powers the Line
-- History chart on the Live Lines page. `lines` above only ever holds the
-- latest open/current snapshot; this table is the append-only trail behind
-- it. Populated by build_db.py scanning every data/raw/lines_*.json file
-- ever pulled (not just the newest one, unlike every other table here) --
-- so the more often pull_lines.py runs, the richer this history gets. A
-- game pulled for the first time only has one point until the next pull.
-- source distinguishes which pull produced a row: 'cfbd' (src/pull_lines.py,
-- via the full pipeline) or 'odds_api' (src/pull_odds_api.py, an independent
-- feed on its own schedule -- see .github/workflows/odds_pull.yml). Kept as a
-- plain column rather than folded into the PRIMARY KEY: two sources landing
-- the exact same (game_id, provider, pulled_at) to the second would require
-- them to run in the very same second, which never happens in practice, and
-- adding it to the key would force a destructive rebuild of this table on
-- every already-populated database. Existing rows (all pre-dating this
-- column) default to 'cfbd' -- see the ALTER TABLE migration in build_db.py
-- for databases created before this column existed.
CREATE TABLE IF NOT EXISTS line_snapshots (
    game_id         BIGINT,
    provider        VARCHAR,
    pulled_at       TIMESTAMP,
    spread          DOUBLE,
    over_under      DOUBLE,
    home_moneyline  DOUBLE,
    away_moneyline  DOUBLE,
    source          VARCHAR DEFAULT 'cfbd',
    PRIMARY KEY (game_id, provider, pulled_at)
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
    talent_diff             DOUBLE,  -- THIS season's recruiting composite, home minus away
    drive_yards_diff        DOUBLE,  -- prior-season yards/drive, home minus away (drive-based features -- see drive_stats_snapshots below)
    drive_points_diff       DOUBLE,  -- prior-season points/drive, home minus away
    drive_turnovers_diff    DOUBLE,  -- prior-season turnovers/drive, home minus away (lower is better -- diff is home's rate minus away's, so negative favors home)
    pass_ypd_diff           DOUBLE,  -- prior-season passing yards/drive, home minus away
    rush_ypd_diff           DOUBLE,  -- prior-season rushing yards/drive, home minus away
    ypa_diff                DOUBLE,  -- prior-season yards/pass attempt, home minus away
    ypc_diff                DOUBLE,  -- prior-season yards/carry, home minus away
    std_down_ppa_diff       DOUBLE,  -- prior-season standard-down net PPA (off minus def-allowed), home minus away -- see situational_stats_snapshots below
    passing_down_ppa_diff   DOUBLE,  -- prior-season passing-down net PPA (off minus def-allowed), home minus away
    red_zone_ppa_diff       DOUBLE,  -- prior-season red-zone net PPA (off minus def-allowed), home minus away
    explosive_rate_diff     DOUBLE   -- prior-season explosive-play net rate (off rate minus def-allowed rate), home minus away
);

-- Raw drive-level data (one row per offensive possession) -- see
-- src/pull_drives.py. id is CFBD's own drive id, taken as-is rather than a
-- composite key -- CFBD guarantees it's unique.
CREATE TABLE IF NOT EXISTS drives (
    id                  BIGINT PRIMARY KEY,
    game_id             BIGINT,
    offense             VARCHAR,
    offense_conference  VARCHAR,
    defense             VARCHAR,
    defense_conference  VARCHAR,
    drive_number        INTEGER,
    scoring             BOOLEAN,
    start_period        INTEGER,
    start_yardline      INTEGER,
    start_yards_to_goal INTEGER,
    end_period          INTEGER,
    end_yardline        INTEGER,
    end_yards_to_goal   INTEGER,
    plays               INTEGER,
    yards               INTEGER,
    drive_result        VARCHAR,
    is_home_offense     BOOLEAN,
    start_offense_score INTEGER,
    start_defense_score INTEGER,
    end_offense_score   INTEGER,
    end_defense_score   INTEGER
);

-- Raw play-by-play data -- see src/pull_plays.py. id is CFBD's own play id.
-- drive_id joins back to drives.id (not a DB-enforced FK -- DuckDB doesn't
-- need one for this project's read patterns, same as everywhere else here).
-- play_type is the raw CFBD string (e.g. "Rush", "Pass Reception", "Sack") --
-- src/build_db.py's classify_play() buckets it into rush/pass/other when
-- computing drive_stats_snapshots below; kept here unclassified/raw so a
-- future reclassification never requires re-pulling data, just rerunning
-- build_db.py.
-- yards_to_goal and ppa (added alongside the down/distance situational-splits
-- feature work -- see situational_stats_snapshots below): both were already
-- present on every play object CFBD's API returns and therefore already
-- sitting in every cached data/raw/plays_w*.json.gz snapshot ever pulled --
-- this is purely extracting two more fields build_plays_table() wasn't
-- reading yet, NOT a new CFBD call. yards_to_goal is the field position
-- (distance to the end zone) at the snap, used for the red-zone split. ppa
-- is CFBD's own per-play predicted-points-added value -- the same
-- opponent-adjusted efficiency metric advanced_stats/ppa_snapshots already
-- use at the season/week level, just at the individual-play grain, which is
-- what makes situational (standard-down/passing-down/red-zone) splits of it
-- possible at all.
CREATE TABLE IF NOT EXISTS plays (
    id              BIGINT PRIMARY KEY,
    drive_id        BIGINT,
    game_id         BIGINT,
    drive_number    INTEGER,
    play_number     INTEGER,
    offense         VARCHAR,
    defense         VARCHAR,
    period          INTEGER,
    down            INTEGER,
    distance        INTEGER,
    yards_to_goal   INTEGER,
    yards_gained    INTEGER,
    play_type       VARCHAR,
    scoring         BOOLEAN,
    ppa             DOUBLE
);

-- Drive-based rate stats, walk-forward-safe, EXACTLY the same (season, team,
-- as_of_week) shape as ppa_snapshots above -- one row per team per
-- week-cutoff, computed ENTIRELY LOCALLY by build_db.py from the drives/plays
-- tables (no extra CFBD call needed, unlike ppa_snapshots' end_week
-- parameter). Two different uses read this same table two different ways:
--   - PRIOR-season feature (the safe default -- see src/features.py's
--     drive_*_diff columns): look up the row with the LAST as_of_week of
--     season - 1 for a team -- that's just that whole prior season's rate,
--     no different in spirit from sp_diff/ppa_diff's prior-season lookup.
--   - IN-season, walk-forward-safe feature (computed but NOT wired into
--     model.FEATURE_COLS by default): as_of_week <= W - 1 within the SAME
--     season being predicted, exactly ppa_snapshots' ASOF-join pattern.
-- pass_yards_per_drive/rush_yards_per_drive/yards_per_attempt/yards_per_carry
-- are NULL for any (season, team, as_of_week) computed before plays data for
-- that season has been pulled (src/pull_plays.py) -- the drive-only stats
-- (yards/points/turnovers per drive, drives/game) don't depend on plays at
-- all and are always populated once drives data exists.
--
-- IMPORTANT CAUTION (see totals_model.py's docstring for the full story):
-- a prior attempt at using raw THIS-season scoring form as a feature made
-- ROI WORSE, because a fast-moving, publicly observable in-season stat is
-- exactly the kind of signal a sportsbook's own line is already pricing in.
-- Drive-based rate stats are just as public and fast-moving, so the
-- in-season version of these features should NOT be trusted or wired into
-- FEATURE_COLS without a real backtest confirming it actually helps first.
--
-- FBS-vs-FBS drives ONLY -- build_drive_stats_snapshots_table() drops any
-- drive where either offense_conference or defense_conference that season
-- isn't in FBS_CONFERENCES (teams.py), read straight off the drives table's
-- own offense_conference/defense_conference columns (CFBD's own drive
-- object, no join needed). Same fix, same reason, as
-- situational_stats_snapshots below: North Dakota State's every pre-2026
-- season is 100% FCS-vs-FCS, and real MW teams' own schedules include an
-- occasional FCS "buy game" -- without this filter, those drives would get
-- folded into a team's season rate stats as if they were FBS-level
-- performance.
CREATE TABLE IF NOT EXISTS drive_stats_snapshots (
    season                  INTEGER,
    team                    VARCHAR,
    as_of_week              INTEGER,
    drives                  INTEGER,
    games                   INTEGER,
    yards_per_drive         DOUBLE,
    points_per_drive        DOUBLE,
    turnovers_per_drive     DOUBLE,
    pass_yards_per_drive    DOUBLE,
    rush_yards_per_drive    DOUBLE,
    yards_per_attempt       DOUBLE,
    yards_per_carry         DOUBLE,
    PRIMARY KEY (season, team, as_of_week)
);

-- Down-and-distance situational splits of per-play PPA -- the attack plan's
-- own top-ranked predictive category (Section 2/"Recommended Predictive
-- Measures": opponent-adjusted efficiency "split by ... down-and-distance
-- situation (standard downs vs. passing downs, red zone)"), computed
-- ENTIRELY LOCALLY by build_db.py from the plays table's yards_to_goal/ppa
-- columns (see the plays table's own comment -- no new CFBD call). Same
-- walk-forward-safe (season, team, as_of_week) shape and same two read
-- patterns as drive_stats_snapshots/ppa_snapshots above (prior-season
-- lookup for the margin model's FEATURE_COLS -- see model.py's
-- std_down_ppa_diff/passing_down_ppa_diff/red_zone_ppa_diff/
-- explosive_rate_diff -- or an in-season ASOF join, not currently wired in
-- for the same "don't trust an untested in-season signal" reason
-- drive_stats_snapshots' own comment gives).
--
-- Standard downs / passing downs use Bill Connelly's own definition (the
-- same SP+ methodology this project already cites as a second-opinion
-- measure in the attack plan) rather than inventing a new split: standard
-- down = 1st down (any distance), or 2nd-and-7-or-less, or 3rd/4th-and-2-
-- or-less; passing down = 2nd-and-8-or-more, or 3rd/4th-and-3-or-more. Red
-- zone = any play snapped with yards_to_goal <= 20. Explosive = a rush
-- gaining 10+ yards or a pass (including sacks/incompletions counted at
-- their actual yards_gained -- see build_db.py's classify_play()) gaining
-- 15+ yards, the same thresholds used across public CFB analytics.
--
-- off_*/def_* is this team's own NET margin in each situation (its offense's
-- PPA/rate in that situation MINUS its defense's PPA/rate ALLOWED in that
-- same situation) -- the same off-minus-def framing ppa_diff already uses
-- overall, just split by situation instead of left as one aggregate number.
--
-- FBS-vs-FBS plays ONLY -- build_situational_stats_snapshots_table() drops
-- any play where either side's conference that season isn't in
-- FBS_CONFERENCES (teams.py). Added after a real backtest A/B
-- (src/diagnose_situational_features.py) traced a Mountain West-involved
-- regression straight to FCS contamination: North Dakota State's every
-- pre-2026 season is 100% FCS-vs-FCS (Missouri Valley Football Conference),
-- and even legitimate FBS MW teams' own schedules include an FCS "buy game"
-- most seasons (Hawai'i vs. Portland State, UTEP vs. Houston Christian,
-- etc.) -- without the filter, those snaps got folded into a team's
-- situational splits as if they were comparable to FBS-level competition,
-- and were the single biggest source of the worst prediction swings.
CREATE TABLE IF NOT EXISTS situational_stats_snapshots (
    season                  INTEGER,
    team                    VARCHAR,
    as_of_week              INTEGER,
    off_plays               INTEGER,   -- offensive snaps counted (sample-size context, not a feature itself)
    def_plays               INTEGER,   -- defensive snaps counted
    off_std_down_ppa        DOUBLE,
    def_std_down_ppa        DOUBLE,
    off_passing_down_ppa    DOUBLE,
    def_passing_down_ppa    DOUBLE,
    off_red_zone_ppa        DOUBLE,
    def_red_zone_ppa        DOUBLE,
    off_explosive_rate      DOUBLE,
    def_explosive_rate      DOUBLE,
    PRIMARY KEY (season, team, as_of_week)
);
