"""
Load the raw JSON snapshots in data/raw/ into the clean DuckDB tables defined
in db/schema.sql. Always loads the LATEST snapshot per (prefix, year) --
re-running a pull script writes a new timestamped file rather than overwriting,
so this picks the freshest one and simply ignores older pulls of the same year.

Team names are normalized through teams.normalize_team_name() on the way in,
so "San Jose State" and "San José State" (etc.) collapse to one canonical row
before anything joins.

Usage:
    source .venv/bin/activate
    python src/build_db.py
"""
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import time

from config import RAW_DIR, DB_PATH, END_YEAR
from raw_storage import load_json_any

# Line movement is only ever interesting for the season currently being
# played -- a past season's games are over and their lines will never move
# again, so there's no reason to keep backfilling snapshot history for
# them. END_YEAR (config.py) is "the current season" throughout this
# project; only its raw lines_<END_YEAR>_*.json snapshots get scanned here.
LINE_HISTORY_SEASON = END_YEAR
from teams import MW_TEAMS_2026, normalize_team_name

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

# CFBD's `lines` endpoint and The Odds API's `bookmakers[].title` are two
# independent upstream sources for the same real-world sportsbooks, and
# neither is internally consistent (nor consistent with the other) about
# spacing/casing for a given book -- "DraftKings" vs "Draft Kings" showed up
# on the live site as two separate book columns because build_lines_table()/
# build_line_snapshots_table()/build_odds_api_snapshots_table() all stored
# whatever raw string the API happened to hand back, with no canonicalization
# at all. normalize_provider() collapses any spelling/casing/spacing variant
# of a known book down to one canonical display name before it ever reaches
# a table -- keyed by a normalized (lowercased, non-alphanumeric characters
# stripped) form of the raw string, so "Draft Kings", "DraftKings", and
# "draftkings" all hash to the same "draftkings" lookup key. An unrecognized
# provider (a book not in this map yet) just passes through unchanged rather
# than being dropped, so a brand new book showing up in the feed never
# silently disappears -- it just won't get deduped against a variant spelling
# until someone adds it here.
PROVIDER_ALIASES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "caesars": "Caesars",
    "caesarssportsbook": "Caesars",
    "pointsbet": "PointsBet",
    "pointsbetus": "PointsBet",
    "espnbet": "ESPN Bet",
    "bovada": "Bovada",
    "betrivers": "BetRivers",
    "williamhill": "William Hill",
    "williamhillus": "William Hill",
    "unibet": "Unibet",
    "wynnbet": "WynnBET",
    "betonlineag": "BetOnline.ag",
    "mybookieag": "MyBookie.ag",
    "superbook": "SuperBook",
    "consensus": "consensus",
    "teamrankings": "teamrankings",
    "numberfire": "numberfire",
}


def normalize_provider(name):
    if not name:
        return name
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return PROVIDER_ALIASES.get(key, name.strip())


def normalize_existing_provider_names(con):
    """
    One-time-per-variant (but always safe to re-run) cleanup for rows written
    before normalize_provider() existed. `lines` never needs this -- it's
    fully DELETE+re-INSERT'd every run by build_lines_table(), so the next
    normal run already fixes it. `line_snapshots` is append-only (INSERT OR
    IGNORE, see build_line_snapshots_table()'s docstring), so an old
    differently-spelled row sits there forever unless renamed explicitly.
    """
    existing = con.execute("SELECT DISTINCT provider FROM line_snapshots").fetchall()
    renames = [(raw, normalize_provider(raw)) for (raw,) in existing if raw and normalize_provider(raw) != raw]
    for raw, canon in renames:
        # A row that would collide with an existing canonical row at the same
        # (game_id, pulled_at) -- the primary key -- is a genuine duplicate
        # pull under two spellings; drop the differently-spelled one before
        # renaming the rest so the UPDATE never trips the primary key.
        con.execute("""
            DELETE FROM line_snapshots
            WHERE provider = ?
              AND EXISTS (
                  SELECT 1 FROM line_snapshots c
                  WHERE c.provider = ? AND c.game_id = line_snapshots.game_id
                    AND c.pulled_at = line_snapshots.pulled_at
              )
        """, [raw, canon])
        con.execute("UPDATE line_snapshots SET provider = ? WHERE provider = ?", [canon, raw])
        print(f"line_snapshots: normalized existing provider '{raw}' -> '{canon}'")

# Rush/pass classification for a raw play_type string -- CFBD's full
# taxonomy has roughly 48 values, and no live API key was available in
# development to call StatsApi.get_play_types() and confirm the exhaustive
# list directly (a third-party reference, cfbfastR's cfbd_play_types table,
# was used to sanity-check the values below, but isn't authoritative).
# Rather than guess at the full list, this only claims the values reasonably
# confirmed, and classify_play() logs (once per distinct value, not once per
# play) anything it doesn't recognize -- so a real rush/pass type missing
# from these sets is loud and discoverable in build_db.py's own output, not
# silently mis-bucketed into "other" forever.
RUSH_PLAY_TYPES = {"Rush", "Rushing Touchdown"}
# Sack is deliberately included in PASS_PLAY_TYPES: a sack is a broken pass
# play, and excluding it would silently drop real (negative) passing
# yardage and understate how often a team's passing game got stopped. This
# is a simplification versus an official box score (which tracks sacks
# separately from completions/attempts) -- acceptable here because these are
# team-level PER-DRIVE RATE features for the model, not a box-score replica.
#
# "Pass Completion"/"Pass" and "Interception"/"Pass Interception Return"
# confirmed from a real ~2M-row plays pull (2016-2026) -- CFBD apparently
# uses these instead of (or alongside, across different seasons) the
# "Pass Reception"/"Interception Return" values originally guessed from a
# third-party reference table. Both spellings are kept since which one
# shows up seems to vary by season/data vintage.
PASS_PLAY_TYPES = {
    "Pass Reception", "Pass Completion", "Pass", "Pass Incompletion", "Passing Touchdown",
    "Sack", "Interception Return", "Interception Return Touchdown",
    "Interception", "Pass Interception Return",
}
# Real plays that are neither a rush nor a pass for these rate stats'
# purposes (special teams, administrative, or a turnover event whose
# play_type doesn't indicate whether it came off a rush or a pass) --
# listed here just so they don't trip classify_play()'s "unrecognized"
# warning. "Fumble" specifically: CFBD's generic fumble play_type doesn't
# say whether the fumble happened on a rush or pass snap, so there's no
# reliable way to attribute it to either -- "other" is the honest answer,
# not a guess.
OTHER_KNOWN_PLAY_TYPES = {
    "Kickoff", "Kickoff Return (Offense)", "Kickoff Return Touchdown",
    "Punt", "Punt Return", "Punt Return Touchdown", "Blocked Punt", "Blocked Punt Touchdown",
    "Field Goal Good", "Field Goal Missed", "Blocked Field Goal", "Blocked Field Goal Touchdown",
    "Missed Field Goal Return", "Missed Field Goal Return Touchdown",
    "Penalty", "Timeout", "End Period", "End of Half", "End of Game", "End of Regulation", "Start of Period",
    "Uncategorized", "placeholder", "Two Point Rush", "Two Point Pass", "Two Point Conversion",
    "Fumble", "Fumble Recovery (Own)", "Fumble Recovery (Opponent)", "Fumble Return Touchdown",
    "Safety", "Defensive 2pt Conversion",
}
_WARNED_PLAY_TYPES = set()


def classify_play(play_type):
    """Returns 'rush', 'pass', or 'other'."""
    if play_type in RUSH_PLAY_TYPES:
        return "rush"
    if play_type in PASS_PLAY_TYPES:
        return "pass"
    if play_type not in OTHER_KNOWN_PLAY_TYPES and play_type not in _WARNED_PLAY_TYPES:
        _WARNED_PLAY_TYPES.add(play_type)
        print(f"  [drive_stats] unrecognized play_type '{play_type}' -- counted as 'other' "
              f"(not rush or pass). Add it to RUSH_PLAY_TYPES/PASS_PLAY_TYPES in build_db.py "
              f"if it should count as one.")
    return "other"


# Matches both the plain .json files every pull script wrote before the
# gzip-compression change, and the new .json.gz files -- see raw_storage.py's
# module docstring. The (?:\.gz)? group is the entire backward-compat trick;
# everything downstream keys off `prefix`/`year`/`stamp`, which don't care
# which extension a given file happens to have.
SNAPSHOT_RE = re.compile(r"^(?P<prefix>.+)_(?P<year>\d{4})_(?P<stamp>\d{8}T\d{6}Z)\.json(?:\.gz)?$")


def _glob_json_any(pattern: str):
    """
    RAW_DIR.glob(), but matching both `<pattern>.json` and `<pattern>.json.gz`
    in one call -- `pattern` should end in ".json" (as if compression didn't
    exist); this expands it to also catch the compressed form.
    """
    assert pattern.endswith(".json")
    return list(RAW_DIR.glob(pattern)) + list(RAW_DIR.glob(pattern + ".gz"))


def latest_snapshots():
    """Group raw files by (prefix, year) and keep only the newest stamp for each."""
    best = {}
    for f in _glob_json_any("*.json"):
        m = SNAPSHOT_RE.match(f.name)
        if not m:
            continue
        key = (m.group("prefix"), int(m.group("year")))
        stamp = m.group("stamp")
        if key not in best or stamp > best[key][0]:
            best[key] = (stamp, f)
    return {key: f for key, (stamp, f) in best.items()}


def load_json(path: Path):
    """Reads either a plain .json snapshot or a gzip-compressed .json.gz one -- see raw_storage.py."""
    return load_json_any(path)


def all_lines_snapshots():
    """
    Unlike latest_snapshots(), this returns EVERY lines_<year>_<stamp>.json(.gz)
    ever pulled, not just the newest one -- that full history is exactly what
    the Line History chart needs. Returns a list of (year, stamp, path),
    oldest first.
    """
    out = []
    for f in _glob_json_any("lines_*.json"):
        m = SNAPSHOT_RE.match(f.name)
        if not m or m.group("prefix") != "lines":
            continue
        out.append((int(m.group("year")), m.group("stamp"), f))
    out.sort(key=lambda t: t[1])
    return out


def build_teams_table(con):
    rows = [
        (name, meta["prior_conference"], meta["joined_2026"], meta["notes"])
        for name, meta in MW_TEAMS_2026.items()
    ]
    con.execute("DELETE FROM teams")
    con.executemany("INSERT INTO teams VALUES (?, ?, ?, ?)", rows)
    print(f"teams: {len(rows)} rows")


def build_games_table(con, snapshots):
    rows = []
    for (prefix, year), path in snapshots.items():
        if prefix not in ("games_fbs", "games_ndsu"):
            continue
        for g in load_json(path):
            rows.append((
                g["id"], g["season"], g["week"], str(g.get("season_type")),
                g.get("start_date"), g.get("completed"), g.get("neutral_site"),
                g.get("conference_game"), g.get("venue"), g.get("venue_id"),
                normalize_team_name(g.get("home_team")), g.get("home_conference"),
                g.get("home_points"), g.get("home_pregame_elo"),
                normalize_team_name(g.get("away_team")), g.get("away_conference"),
                g.get("away_points"), g.get("away_pregame_elo"),
                g.get("excitement_index"),
            ))
    if not rows:
        print("games: no raw snapshots found yet (run src/pull_games.py first)")
        return
    con.execute("DELETE FROM games")
    con.executemany(
        "INSERT OR REPLACE INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    print(f"games: {len(rows)} rows")


def build_advanced_stats_table(con, snapshots):
    rows = []
    for (prefix, year), path in snapshots.items():
        if prefix != "advanced_stats":
            continue
        for a in load_json(path):
            off, dfn = a.get("offense") or {}, a.get("defense") or {}
            rows.append((
                a["season"], normalize_team_name(a.get("team")), a.get("conference"),
                off.get("ppa"), off.get("success_rate"), off.get("explosiveness"), off.get("plays"),
                dfn.get("ppa"), dfn.get("success_rate"), dfn.get("explosiveness"),
            ))
    if not rows:
        print("advanced_stats: no raw snapshots found yet (run src/pull_stats.py first)")
        return
    con.execute("DELETE FROM advanced_stats")
    # Explicit column list, not a bare "VALUES (?,?,...)" -- off_plays was
    # added to this table via a mid-list ALTER TABLE ADD COLUMN (see main()),
    # which always appends the new column at the table's PHYSICAL end. On
    # any database where advanced_stats already existed before that
    # migration, the table's real on-disk column order no longer matches
    # this row tuple's schema.sql-declared order (off_plays 7th here, but
    # last physically) -- and a bare, column-list-less INSERT matches by
    # position, so it would silently scramble off_ppa/def_ppa/off_plays
    # (and everything after) into the wrong columns with NO error, unlike
    # the same-shaped bug in build_plays_table() above (which at least
    # crashed loudly on a type mismatch). Naming every column here makes the
    # insert match by NAME instead, correct regardless of physical order.
    con.executemany(
        "INSERT OR REPLACE INTO advanced_stats "
        "(season, team, conference, off_ppa, off_success_rate, off_explosiveness, off_plays, "
        "def_ppa, def_success_rate, def_explosiveness) VALUES (?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    print(f"advanced_stats: {len(rows)} rows")


DRIVES_COLUMNS = [
    "id", "game_id", "offense", "offense_conference", "defense", "defense_conference",
    "drive_number", "scoring", "start_period", "start_yardline", "start_yards_to_goal",
    "end_period", "end_yardline", "end_yards_to_goal", "plays", "yards", "drive_result",
    "is_home_offense", "start_offense_score", "start_defense_score",
    "end_offense_score", "end_defense_score",
]
PLAYS_COLUMNS = [
    "id", "drive_id", "game_id", "drive_number", "play_number", "offense", "defense",
    "period", "down", "distance", "yards_to_goal", "yards_gained", "play_type", "scoring", "ppa",
]


def build_drives_table(con, snapshots):
    """
    Bulk DataFrame insert (register + INSERT ... SELECT), NOT executemany --
    a full-history drives backfill is on the order of 100,000+ rows (every
    FBS offensive possession across 11 seasons), and executemany's per-row
    round trip is slow enough at that scale to matter (games/advanced_stats/
    etc. get away with executemany because they're 1-2 orders of magnitude
    smaller). Same bulk-insert trick features.py's build_features() already
    uses for game_features.
    """
    rows = []
    for (prefix, year), path in snapshots.items():
        if prefix != "drives":
            continue
        for d in load_json(path):
            rows.append((
                d["id"], d.get("game_id"),
                normalize_team_name(d.get("offense")), d.get("offense_conference"),
                normalize_team_name(d.get("defense")), d.get("defense_conference"),
                d.get("drive_number"), d.get("scoring"),
                d.get("start_period"), d.get("start_yardline"), d.get("start_yards_to_goal"),
                d.get("end_period"), d.get("end_yardline"), d.get("end_yards_to_goal"),
                d.get("plays"), d.get("yards"), d.get("drive_result"),
                d.get("is_home_offense"),
                d.get("start_offense_score"), d.get("start_defense_score"),
                d.get("end_offense_score"), d.get("end_defense_score"),
            ))
    if not rows:
        print("drives: no raw snapshots found yet (run src/pull_drives.py first)")
        return
    df = pd.DataFrame(rows, columns=DRIVES_COLUMNS)
    con.execute("DELETE FROM drives")
    con.register("df_drives_bulk", df)
    con.execute("INSERT INTO drives SELECT * FROM df_drives_bulk")
    con.unregister("df_drives_bulk")
    print(f"drives: {len(df)} rows")


PLAYS_PREFIX_RE = re.compile(r"^plays_w(?P<week>\d+)$")


def build_plays_table(con, snapshots):
    """
    Unlike every other table here (one row per season), plays_*.json files
    are keyed by (season, WEEK) -- see pull_plays.py's docstring for why
    (CFBD's PlaysApi.get_plays requires a week, unlike get_drives). The week
    number lives in the filename's prefix (plays_w<NN>_<season>_<stamp>.json),
    the exact same trick build_ppa_snapshots_table() already uses for its own
    per-week files, so latest_snapshots()'s normal (prefix, year) grouping
    already does the right thing here with no extra code needed.
    """
    rows = []
    for (prefix, year), path in snapshots.items():
        if not PLAYS_PREFIX_RE.match(prefix):
            continue
        for p in load_json(path):
            rows.append((
                p["id"], p.get("drive_id"), p.get("game_id"),
                p.get("drive_number"), p.get("play_number"),
                normalize_team_name(p.get("offense")), normalize_team_name(p.get("defense")),
                p.get("period"), p.get("down"), p.get("distance"),
                p.get("yards_to_goal"), p.get("yards_gained"), p.get("play_type"), p.get("scoring"),
                p.get("ppa"),
            ))
    if not rows:
        print("plays: no raw snapshots found yet (run src/pull_plays.py first)")
        return
    # Bulk DataFrame insert, not executemany -- a full-history backfill is
    # 1M+ rows (every play of every FBS game across 11 seasons), where
    # executemany's per-row overhead is genuinely slow. See build_drives_table's
    # docstring for the same reasoning.
    #
    # "INSERT INTO plays BY NAME", not a plain "INSERT INTO plays SELECT *" --
    # a bare SELECT * matches columns by POSITION, which silently breaks on
    # any database where `plays` already existed before yards_to_goal/ppa
    # were added: ALTER TABLE ADD COLUMN always appends new columns at the
    # END of the table's physical column order, but PLAYS_COLUMNS/schema.sql
    # both declare yards_to_goal/ppa in the MIDDLE (right after distance).
    # On such a database the two column orders no longer line up position-
    # for-position, and a positional INSERT scrambles columns -- e.g.
    # play_type (a string) lands in the physical `scoring` (BOOLEAN) slot,
    # which crashes with "Could not convert string 'Rush' to BOOL" the
    # instant it hits a real row. BY NAME matches each DataFrame column to
    # the table column with the same name regardless of either one's
    # physical order, so this is correct whether `plays` was just freshly
    # created from schema.sql (columns already in the right order) or is an
    # older table that picked up yards_to_goal/ppa via the ALTER TABLE
    # migration in main() (appended at the end).
    df = pd.DataFrame(rows, columns=PLAYS_COLUMNS)
    con.execute("DELETE FROM plays")
    con.register("df_plays_bulk", df)
    con.execute("INSERT INTO plays BY NAME SELECT * FROM df_plays_bulk")
    con.unregister("df_plays_bulk")
    print(f"plays: {len(df)} rows")


def build_drive_stats_snapshots_table(con):
    """
    Computed entirely from the drives/plays tables already loaded by
    build_drives_table()/build_plays_table() above -- no raw JSON scanned
    directly here, unlike every other build_*_table(). One row per (season,
    team, as_of_week): that offense's cumulative drive-based rate stats
    across every one of its drives in `season` through week as_of_week
    (inclusive) -- see the drive_stats_snapshots comment in schema.sql for
    the two different ways this table gets read later (prior-season lookup
    in features.py vs. an in-season ASOF join, mirroring ppa_snapshots).

    Runs even if `plays` is empty (drives-only rate stats -- yards/points/
    turnovers per drive, drives/game -- don't need play-by-play data at
    all); pass_yards_per_drive/rush_yards_per_drive/yards_per_attempt/
    yards_per_carry just stay NULL until src/pull_plays.py has been run.
    """
    drives = con.execute("""
        SELECT d.id AS drive_id, d.game_id, d.offense, d.plays, d.yards, d.drive_result,
               d.start_offense_score, d.end_offense_score,
               g.season, g.week
        FROM drives d
        JOIN games g ON g.game_id = d.game_id
        WHERE g.season IS NOT NULL AND g.week IS NOT NULL AND d.offense IS NOT NULL
    """).fetchdf()
    if drives.empty:
        print("drive_stats_snapshots: no drives joined to a known game yet "
              "(run src/pull_drives.py, then rerun build_db.py)")
        return

    plays = con.execute("SELECT drive_id, yards_gained, play_type FROM plays").fetchdf()
    have_plays = not plays.empty
    if have_plays:
        plays = plays.copy()
        plays["kind"] = plays["play_type"].map(classify_play)
        plays["yards_gained"] = pd.to_numeric(plays["yards_gained"], errors="coerce").fillna(0)
        for kind, yards_col, n_col in [("rush", "rush_yards", "rush_plays"), ("pass", "pass_yards", "pass_plays")]:
            sub = plays[plays["kind"] == kind]
            drives = drives.merge(sub.groupby("drive_id")["yards_gained"].sum().rename(yards_col),
                                   left_on="drive_id", right_index=True, how="left")
            drives = drives.merge(sub.groupby("drive_id").size().rename(n_col),
                                   left_on="drive_id", right_index=True, how="left")
    else:
        drives["rush_yards"] = drives["rush_plays"] = drives["pass_yards"] = drives["pass_plays"] = None
        print("drive_stats_snapshots: no plays loaded yet -- pass/rush split columns will be NULL "
              "(run src/pull_plays.py to fill them in)")

    # Turnover-ending drive detection: match CFBD's drive_result values
    # (e.g. "FUMBLE", "FUMBLE TD", "INT", "INT TD") by substring rather than
    # an exact enum list, same defensive approach as classify_play().
    drives["is_turnover"] = drives["drive_result"].fillna("").str.upper().str.contains("FUMBLE|INT")
    drives["yards"] = pd.to_numeric(drives["yards"], errors="coerce").fillna(0)
    drives["drive_points"] = (
        pd.to_numeric(drives["end_offense_score"], errors="coerce")
        - pd.to_numeric(drives["start_offense_score"], errors="coerce")
    ).fillna(0)

    rows = []
    n_team_seasons = 0
    for (season, team), grp in drives.groupby(["season", "offense"]):
        n_team_seasons += 1
        grp = grp.sort_values("week")
        for as_of_week in sorted(grp["week"].unique()):
            window = grp[grp["week"] <= as_of_week]
            n_drives = len(window)
            if n_drives == 0:
                continue
            n_games = window["game_id"].nunique()
            pass_yards_sum = window["pass_yards"].sum() if have_plays else None
            pass_plays_sum = window["pass_plays"].sum() if have_plays else None
            rush_yards_sum = window["rush_yards"].sum() if have_plays else None
            rush_plays_sum = window["rush_plays"].sum() if have_plays else None
            rows.append((
                int(season), team, int(as_of_week),
                int(n_drives), int(n_games),
                float(window["yards"].sum() / n_drives),
                float(window["drive_points"].sum() / n_drives),
                float(window["is_turnover"].sum() / n_drives),
                (float(pass_yards_sum / n_drives) if have_plays and pd.notna(pass_yards_sum) else None),
                (float(rush_yards_sum / n_drives) if have_plays and pd.notna(rush_yards_sum) else None),
                (float(pass_yards_sum / pass_plays_sum) if have_plays and pass_plays_sum else None),
                (float(rush_yards_sum / rush_plays_sum) if have_plays and rush_plays_sum else None),
            ))

    if not rows:
        print("drive_stats_snapshots: nothing computed")
        return
    con.execute("DELETE FROM drive_stats_snapshots")
    con.executemany(
        "INSERT OR REPLACE INTO drive_stats_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    print(f"drive_stats_snapshots: {len(rows)} rows ({n_team_seasons} team-seasons)")


def build_situational_stats_snapshots_table(con):
    """
    Down-and-distance situational splits of per-play PPA -- see the
    situational_stats_snapshots comment in schema.sql for the full column
    semantics and the standard-down/passing-down/red-zone/explosive
    definitions used below. Computed entirely from the plays table already
    loaded by build_plays_table() above (joined to games for season/week),
    same walk-forward-safe (season, team, as_of_week) shape as
    drive_stats_snapshots/ppa_snapshots.

    Unlike drive_stats_snapshots (offense-only -- a team's own drive
    efficiency), this tracks BOTH sides: off_* is this team's own PPA/rate
    when it had the ball in that situation, def_* is the PPA/rate it
    ALLOWED (the opposing offense's PPA) when it was on defense in that same
    situation. model.py's *_diff columns combine the two sides (home's
    off-minus-def net rating vs. away's) at feature-build time -- this table
    just stores the raw two-sided splits.

    Situation/red-zone/explosive classification is vectorized (np.select,
    boolean masks) over the whole plays frame rather than a row-wise
    .apply -- a full-history plays table is 1M+ rows, and .apply at that
    size is slow enough to matter (same reasoning classify_play()'s caller
    already applies via .map() instead of a Python loop). The walk-forward
    accumulation itself uses a per-(season, team, week) aggregate followed
    by groupby().cumsum(), not the nested "filter window per as_of_week"
    loop drive_stats_snapshots uses -- cumsum gives the identical
    walk-forward-safe result (each as_of_week's total is strictly plays
    through that week, nothing from later weeks) in one pass instead of one
    re-filter per week, which matters at this row count.

    Skipped entirely (prints and returns) if plays is empty or has no
    rush/pass snaps -- unlike drive_stats_snapshots, there's no
    reduced-columns fallback here; every column this table has requires
    play-by-play down/distance/yards_to_goal/ppa data, so there's nothing
    useful to compute without it (run src/pull_plays.py, then rerun
    build_db.py).
    """
    plays = con.execute("""
        SELECT p.offense, p.defense, p.down, p.distance, p.yards_to_goal,
               p.yards_gained, p.play_type, p.ppa,
               g.season, g.week
        FROM plays p
        JOIN games g ON g.game_id = p.game_id
        WHERE g.season IS NOT NULL AND g.week IS NOT NULL
              AND p.offense IS NOT NULL AND p.defense IS NOT NULL
    """).fetchdf()
    if plays.empty:
        print("situational_stats_snapshots: no plays joined to a known game yet "
              "(run src/pull_plays.py, then rerun build_db.py)")
        return

    plays["kind"] = plays["play_type"].map(classify_play)
    plays = plays[plays["kind"].isin(("rush", "pass"))].copy()
    if plays.empty:
        print("situational_stats_snapshots: no rush/pass plays found")
        return

    # .astype("float64") after to_numeric, not just to_numeric alone --
    # DuckDB's fetchdf() hands back an INTEGER column with any NULLs as
    # pandas' nullable Int32 dtype, which to_numeric() preserves as-is;
    # comparisons on that dtype (down == 1, etc.) return pandas' nullable
    # "boolean" extension dtype (with pd.NA for unknown rows) instead of a
    # plain numpy bool ndarray, and np.select() below rejects that outright
    # ("invalid entry in condlist: should be boolean ndarray"). Forcing
    # float64 here converts NULL/pd.NA to a normal NaN and every downstream
    # comparison back to a plain numpy bool array.
    plays["down"] = pd.to_numeric(plays["down"], errors="coerce").astype("float64")
    plays["distance"] = pd.to_numeric(plays["distance"], errors="coerce").astype("float64")
    plays["yards_to_goal"] = pd.to_numeric(plays["yards_to_goal"], errors="coerce").astype("float64")
    plays["yards_gained"] = pd.to_numeric(plays["yards_gained"], errors="coerce").fillna(0).astype("float64")
    plays["ppa"] = pd.to_numeric(plays["ppa"], errors="coerce").astype("float64")

    # Bill Connelly's SP+ standard-down/passing-down split (see schema.sql's
    # comment for the full citation): standard = 1st down (any distance), or
    # 2nd-and-7-or-less, or 3rd/4th-and-2-or-less; passing = 2nd-and-8-or-
    # more, or 3rd/4th-and-3-or-more. A play with no recorded down/distance
    # (down is NaN, e.g. some early-season or malformed CFBD rows) falls
    # into "neither" via np.select's default and is excluded from both
    # splits rather than guessed at.
    is_standard = (
        (plays["down"] == 1)
        | ((plays["down"] == 2) & (plays["distance"] <= 7))
        | (plays["down"].isin((3, 4)) & (plays["distance"] <= 2))
    )
    is_passing_down = (
        ((plays["down"] == 2) & (plays["distance"] >= 8))
        | (plays["down"].isin((3, 4)) & (plays["distance"] >= 3))
    )
    plays["situation"] = np.select(
        [is_standard, is_passing_down], ["standard", "passing"], default="neither"
    )

    # Red zone: any scrimmage play snapped with 20 or fewer yards to the end
    # zone. Explosive: a rush gaining 10+ yards, or a pass (including sacks/
    # incompletions counted at their actual yards_gained -- see
    # classify_play()'s "sack is a broken pass play" convention) gaining
    # 15+ yards -- the same thresholds used across public CFB analytics.
    plays["is_red_zone"] = plays["yards_to_goal"] <= 20
    plays["is_explosive"] = (
        ((plays["kind"] == "rush") & (plays["yards_gained"] >= 10))
        | ((plays["kind"] == "pass") & (plays["yards_gained"] >= 15))
    )
    plays["std_ppa"] = plays["ppa"].where(plays["situation"] == "standard")
    plays["passing_ppa"] = plays["ppa"].where(plays["situation"] == "passing")
    plays["rz_ppa"] = plays["ppa"].where(plays["is_red_zone"])
    plays["explosive_flag"] = plays["is_explosive"].astype(int)

    def _safe_div(num, den):
        with np.errstate(divide="ignore", invalid="ignore"):
            ratio = num / den
        return np.where(den > 0, ratio, np.nan)

    def _side_stats(side_col, prefix):
        """
        side_col: "offense" or "defense" -- which column names the team for
        this side of the snap. prefix: "off" or "def" -- the column prefix
        in the returned frame. Returns one row per (season, team,
        as_of_week) with <prefix>_plays/<prefix>_std_down_ppa/
        <prefix>_passing_down_ppa/<prefix>_red_zone_ppa/
        <prefix>_explosive_rate, each a strictly-through-that-week
        cumulative total/rate.
        """
        df = plays.rename(columns={side_col: "team"})
        weekly = df.groupby(["season", "team", "week"]).agg(
            plays_n=("kind", "size"),
            std_ppa_sum=("std_ppa", "sum"),
            std_ppa_n=("std_ppa", "count"),
            passing_ppa_sum=("passing_ppa", "sum"),
            passing_ppa_n=("passing_ppa", "count"),
            rz_ppa_sum=("rz_ppa", "sum"),
            rz_ppa_n=("rz_ppa", "count"),
            explosive_sum=("explosive_flag", "sum"),
        ).reset_index()
        weekly = weekly.sort_values(["season", "team", "week"])

        cum_cols = ["plays_n", "std_ppa_sum", "std_ppa_n", "passing_ppa_sum",
                    "passing_ppa_n", "rz_ppa_sum", "rz_ppa_n", "explosive_sum"]
        cum = weekly.groupby(["season", "team"])[cum_cols].cumsum()
        cum.columns = [f"cum_{c}" for c in cum_cols]
        weekly = pd.concat([weekly[["season", "team", "week"]], cum], axis=1)

        out = pd.DataFrame({
            "season": weekly["season"],
            "team": weekly["team"],
            "as_of_week": weekly["week"],
            f"{prefix}_plays": weekly["cum_plays_n"].astype(int),
            f"{prefix}_std_down_ppa": _safe_div(weekly["cum_std_ppa_sum"], weekly["cum_std_ppa_n"]),
            f"{prefix}_passing_down_ppa": _safe_div(weekly["cum_passing_ppa_sum"], weekly["cum_passing_ppa_n"]),
            f"{prefix}_red_zone_ppa": _safe_div(weekly["cum_rz_ppa_sum"], weekly["cum_rz_ppa_n"]),
            f"{prefix}_explosive_rate": _safe_div(weekly["cum_explosive_sum"], weekly["cum_plays_n"]),
        })
        return out

    off_df = _side_stats("offense", "off")
    def_df = _side_stats("defense", "def")
    merged = off_df.merge(def_df, on=["season", "team", "as_of_week"], how="outer")

    rows = []
    for r in merged.itertuples(index=False):
        rows.append((
            int(r.season), r.team, int(r.as_of_week),
            None if pd.isna(r.off_plays) else int(r.off_plays),
            None if pd.isna(r.def_plays) else int(r.def_plays),
            None if pd.isna(r.off_std_down_ppa) else float(r.off_std_down_ppa),
            None if pd.isna(r.def_std_down_ppa) else float(r.def_std_down_ppa),
            None if pd.isna(r.off_passing_down_ppa) else float(r.off_passing_down_ppa),
            None if pd.isna(r.def_passing_down_ppa) else float(r.def_passing_down_ppa),
            None if pd.isna(r.off_red_zone_ppa) else float(r.off_red_zone_ppa),
            None if pd.isna(r.def_red_zone_ppa) else float(r.def_red_zone_ppa),
            None if pd.isna(r.off_explosive_rate) else float(r.off_explosive_rate),
            None if pd.isna(r.def_explosive_rate) else float(r.def_explosive_rate),
        ))

    if not rows:
        print("situational_stats_snapshots: nothing computed")
        return
    con.execute("DELETE FROM situational_stats_snapshots")
    con.executemany(
        "INSERT OR REPLACE INTO situational_stats_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    print(f"situational_stats_snapshots: {len(rows)} rows")


PPA_SNAPSHOT_PREFIX_RE = re.compile(r"^ppa_snapshot_w(?P<week>\d+)$")


def build_ppa_snapshots_table(con, snapshots):
    """
    Unlike every other build_*_table() here, this one's source files are
    keyed by (season, WEEK) rather than just (season) -- see the
    ppa_snapshots comment in schema.sql and totals_model.py's in-season PPA
    feature. The week number lives in the prefix itself
    (ppa_snapshot_w<NN>_<season>_<stamp>.json), so latest_snapshots()'s
    normal (prefix, year) grouping already does exactly the right thing --
    one entry per (season, week), keeping only the newest re-pull of each --
    with no special-casing needed the way all_lines_snapshots() required.
    """
    rows = []
    for (prefix, year), path in snapshots.items():
        m = PPA_SNAPSHOT_PREFIX_RE.match(prefix)
        if not m:
            continue
        week = int(m.group("week"))
        for a in load_json(path):
            off, dfn = a.get("offense") or {}, a.get("defense") or {}
            rows.append((
                a["season"], normalize_team_name(a.get("team")), week,
                off.get("ppa"), off.get("success_rate"), off.get("explosiveness"),
                dfn.get("ppa"), dfn.get("success_rate"), dfn.get("explosiveness"),
            ))
    if not rows:
        print("ppa_snapshots: no raw snapshots found yet (run src/backfill_ppa_snapshots.py first)")
        return
    con.execute("DELETE FROM ppa_snapshots")
    con.executemany("INSERT OR REPLACE INTO ppa_snapshots VALUES (?,?,?,?,?,?,?,?,?)", rows)
    print(f"ppa_snapshots: {len(rows)} rows")


def build_sp_ratings_table(con, snapshots):
    rows = []
    for (prefix, year), path in snapshots.items():
        if prefix != "sp_ratings":
            continue
        for s in load_json(path):
            off, dfn = s.get("offense") or {}, s.get("defense") or {}
            rows.append((
                s["year"], normalize_team_name(s.get("team")), s.get("conference"),
                s.get("rating"), s.get("ranking"),
                off.get("rating"), dfn.get("rating"),
                (s.get("special_teams") or {}).get("rating"),
            ))
    if not rows:
        print("sp_ratings: no raw snapshots found yet (run src/pull_stats.py first)")
        return
    con.execute("DELETE FROM sp_ratings")
    con.executemany("INSERT OR REPLACE INTO sp_ratings VALUES (?,?,?,?,?,?,?,?)", rows)
    print(f"sp_ratings: {len(rows)} rows")


def build_elo_ratings_table(con, snapshots):
    rows = []
    for (prefix, year), path in snapshots.items():
        if prefix != "elo_ratings":
            continue
        for e in load_json(path):
            rows.append((
                e.get("year", year), normalize_team_name(e.get("team")),
                e.get("conference"), e.get("elo"),
            ))
    if not rows:
        print("elo_ratings: no raw snapshots found yet (run src/pull_stats.py first)")
        return
    con.execute("DELETE FROM elo_ratings")
    con.executemany("INSERT OR REPLACE INTO elo_ratings VALUES (?,?,?,?)", rows)
    print(f"elo_ratings: {len(rows)} rows")


def build_recruiting_table(con, snapshots):
    rows = []
    for (prefix, year), path in snapshots.items():
        if prefix != "recruiting":
            continue
        for r in load_json(path):
            rows.append((
                r.get("year", year), normalize_team_name(r.get("team")),
                r.get("rank"), r.get("points"),
            ))
    if not rows:
        print("recruiting: no raw snapshots found yet (run src/pull_stats.py first)")
        return
    con.execute("DELETE FROM recruiting")
    con.executemany("INSERT OR REPLACE INTO recruiting VALUES (?,?,?,?)", rows)
    print(f"recruiting: {len(rows)} rows")


def _parse_elevation(raw):
    """Venue.elevation comes back as a string, in meters, sometimes empty."""
    if not raw:
        return None
    try:
        meters = float(raw)
    except (TypeError, ValueError):
        return None
    return meters * 3.28084


def build_venues_table(con):
    # Venues are static (not year-partitioned), so they're pulled and matched
    # separately from the per-year snapshot mechanism above.
    files = sorted(_glob_json_any("venues_static_*.json"))
    if not files:
        print("venues: no raw snapshot found yet (run src/pull_venues.py first)")
        return
    rows = []
    for v in load_json(files[-1]):
        rows.append((
            v["id"], v.get("name"), v.get("city"), v.get("state"),
            v.get("latitude"), v.get("longitude"), _parse_elevation(v.get("elevation")),
            v.get("dome"), v.get("grass"), v.get("timezone"),
        ))
    con.execute("DELETE FROM venues")
    con.executemany("INSERT OR REPLACE INTO venues VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"venues: {len(rows)} rows")


def build_lines_table(con, snapshots):
    rows = []
    for (prefix, year), path in snapshots.items():
        if prefix != "lines":
            continue
        for g in load_json(path):
            for line in g.get("lines") or []:
                rows.append((
                    g["id"], g["season"], g["week"],
                    normalize_team_name(g.get("home_team")),
                    normalize_team_name(g.get("away_team")),
                    normalize_provider(line.get("provider")), line.get("spread"), line.get("spread_open"),
                    line.get("over_under"), line.get("over_under_open"),
                    line.get("home_moneyline"), line.get("away_moneyline"),
                ))
    if not rows:
        print("lines: no raw snapshots found yet (run src/pull_lines.py first)")
        return
    con.execute("DELETE FROM lines")
    con.executemany("INSERT OR REPLACE INTO lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"lines: {len(rows)} rows")


def build_line_snapshots_table(con):
    """
    Append-only: every lines_<LINE_HISTORY_SEASON>_*.json ever pulled, not
    just the latest, one row per (game, provider, pulled_at). Deliberately
    scoped to just the current season -- see LINE_HISTORY_SEASON above --
    since a past season's lines are frozen forever and backfilling their
    snapshot history is pure wasted work and disk space, not a real feature.
    Never DELETEs -- INSERT OR IGNORE means re-running build_db.py against
    files already loaded is a safe no-op, and a brand new pull just adds
    whatever's genuinely new. Tagged source='cfbd' -- see
    build_odds_api_snapshots_table() below for the other feed into this
    same table.
    """
    rows = []
    for year, stamp, path in all_lines_snapshots():
        if year != LINE_HISTORY_SEASON:
            continue
        pulled_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
        for g in load_json(path):
            for line in g.get("lines") or []:
                rows.append((
                    g["id"], normalize_provider(line.get("provider")), pulled_at,
                    line.get("spread"), line.get("over_under"),
                    line.get("home_moneyline"), line.get("away_moneyline"),
                    "cfbd",
                ))
    if not rows:
        print("line_snapshots: no raw snapshots found yet (run src/pull_lines.py first)")
        return
    con.executemany("INSERT OR IGNORE INTO line_snapshots VALUES (?,?,?,?,?,?,?,?)", rows)
    total = con.execute("SELECT COUNT(*) FROM line_snapshots WHERE source = 'cfbd'").fetchone()[0]
    print(f"line_snapshots (cfbd): {len(rows)} rows scanned, {total} total in history")


def _ascii(s):
    """Strip diacritics for a plain-ASCII, lowercase comparison -- The Odds
    API tends to spell 'San Jose State' without the accent CFBD uses."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _team_name_matches(raw_name, canonical_name):
    """
    True if an Odds-API-style 'School Mascot' name (e.g. 'Boise State
    Broncos') plausibly refers to our canonical CFBD-style school name (e.g.
    'Boise State'). The Odds API has no shared game_id with CFBD, so events
    have to be matched by name -- this is deliberately loose (startswith/
    contains, not exact-equal) since the mascot suffix and accent handling
    would otherwise cause silent misses.
    """
    if not raw_name or not canonical_name:
        return False
    raw_a, canon_a = _ascii(raw_name), _ascii(canonical_name)
    return raw_a.startswith(canon_a) or canon_a in raw_a


def _mw_game_candidates(con):
    """
    (game_id, home_team, away_team, start_date) for every current-season MW
    matchup -- the match target for Odds API events (see
    match_odds_event_to_game below). Scoped to MW games only, same rule as
    everywhere else in this project (mw_game(), MW_TEAMS_2026) -- it's all
    this site tracks, and keeping the candidate list small is what makes
    date + name matching unambiguous instead of a fuzzy mess across 130+ FBS
    teams.
    """
    from teams import MW_TEAMS_2026
    rows = con.execute("""
        SELECT game_id, home_team, away_team, start_date
        FROM games WHERE season = ?
    """, [LINE_HISTORY_SEASON]).fetchall()
    return [
        (gid, home, away, start)
        for gid, home, away, start in rows
        if home in MW_TEAMS_2026 or away in MW_TEAMS_2026
    ]


def match_odds_event_to_game(candidates, home_raw, away_raw, commence_time):
    """
    Find which CFBD game_id an Odds API event refers to. Matches on
    same-day-ish kickoff (+/- 1 day, to absorb UTC-boundary edge cases --
    this only feeds display history, not the model, so loose tolerance here
    is fine) plus both team names resolving via _team_name_matches().
    Returns (game_id, swapped) where swapped=True means the Odds API listed
    home/away opposite of our games table -- the caller flips the spread
    sign and swaps the moneyline pair accordingly. Returns (None, False) if
    nothing lines up (game not in our table yet, or an unrecognized name).
    """
    for gid, home, away, start in candidates:
        if abs((commence_time.date() - start.date()).days) > 1:
            continue
        if _team_name_matches(home_raw, home) and _team_name_matches(away_raw, away):
            return gid, False
        if _team_name_matches(home_raw, away) and _team_name_matches(away_raw, home):
            return gid, True
    return None, False


def build_odds_api_snapshots_table(con):
    """
    Same idea as build_line_snapshots_table, but sourced from The Odds API
    (see src/pull_odds_api.py) instead of CFBD -- a second, independent line
    feed on its own schedule/quota (default Sun/Wed/Fri -- see
    .github/workflows/odds_pull.yml), tagged source='odds_api' so it's never
    confused with CFBD's own rows in this same table. Display only, same as
    every other line_snapshots row -- model.py/backtest.py never read this
    table at all, only the `lines` table's CFBD data.
    """
    files = sorted(_glob_json_any(f"odds_api_{LINE_HISTORY_SEASON}_*.json"))
    if not files:
        print("line_snapshots (odds_api): no raw snapshots found yet (run src/pull_odds_api.py first)")
        return
    candidates = _mw_game_candidates(con)
    rows, unmatched = [], 0
    for path in files:
        m = SNAPSHOT_RE.match(path.name)
        pulled_at = datetime.strptime(m.group("stamp"), "%Y%m%dT%H%M%SZ")
        for event in load_json(path):
            try:
                commence = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00")).replace(tzinfo=None)
            except (KeyError, ValueError):
                continue
            gid, swapped = match_odds_event_to_game(
                candidates, event.get("home_team"), event.get("away_team"), commence
            )
            if gid is None:
                unmatched += 1
                continue
            home_name = event["home_team"]
            for book in event.get("bookmakers") or []:
                provider = normalize_provider(book.get("title") or book.get("key"))
                spread = total = home_ml = away_ml = None
                for market in book.get("markets") or []:
                    key = market.get("key")
                    outcomes = market.get("outcomes") or []
                    if key == "spreads":
                        for o in outcomes:
                            if _team_name_matches(o.get("name"), home_name):
                                spread = o.get("point")
                    elif key == "totals":
                        for o in outcomes:
                            if o.get("name") == "Over":
                                total = o.get("point")
                    elif key == "h2h":
                        for o in outcomes:
                            if _team_name_matches(o.get("name"), home_name):
                                home_ml = o.get("price")
                            else:
                                away_ml = o.get("price")
                if swapped:
                    spread = -spread if spread is not None else None
                    home_ml, away_ml = away_ml, home_ml
                rows.append((gid, provider, pulled_at, spread, total, home_ml, away_ml, "odds_api"))
    if unmatched:
        print(f"line_snapshots (odds_api): {unmatched} event(s) could not be matched to a tracked MW game (skipped)")
    if not rows:
        print("line_snapshots (odds_api): no matching MW games found in pulled data")
        return
    con.executemany("INSERT OR IGNORE INTO line_snapshots VALUES (?,?,?,?,?,?,?,?)", rows)
    total = con.execute("SELECT COUNT(*) FROM line_snapshots WHERE source = 'odds_api'").fetchone()[0]
    print(f"line_snapshots (odds_api): {len(rows)} rows scanned, {total} total in history")


def main():
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA_PATH.read_text())
    # Migration for databases created before the Odds API integration existed
    # -- schema.sql's CREATE TABLE IF NOT EXISTS above is a no-op on a table
    # that already exists, so a pre-existing line_snapshots table needs this
    # column added explicitly. No-op (and safe to run every time) once it's
    # already there.
    con.execute("ALTER TABLE line_snapshots ADD COLUMN IF NOT EXISTS source VARCHAR DEFAULT 'cfbd'")
    # Migration for databases created before the pace/tempo feature existed --
    # see totals_model.py's pace_sum feature and the off_plays comment in
    # schema.sql. Full season-total offensive snaps; totals_model.py divides
    # by that season's games played to get a plays/game rate.
    con.execute("ALTER TABLE advanced_stats ADD COLUMN IF NOT EXISTS off_plays INTEGER")
    # Migration for databases created before the down/distance situational-
    # splits feature existed -- see the plays table's own comment in
    # schema.sql. Both columns were already present on every cached raw
    # plays_w*.json.gz snapshot (CFBD's Play object always had them); this
    # migration just lets an existing `plays` table start storing them on
    # the next build_db.py run, no re-pull needed.
    con.execute("ALTER TABLE plays ADD COLUMN IF NOT EXISTS yards_to_goal INTEGER")
    con.execute("ALTER TABLE plays ADD COLUMN IF NOT EXISTS ppa DOUBLE")

    build_teams_table(con)

    snapshots = latest_snapshots()
    if not snapshots:
        print(
            "\nNo raw data pulled yet -- this is expected before you've set a CFBD API "
            "key and run pull_games.py / pull_stats.py / pull_lines.py. The DB now has "
            "its schema and the 2026 team reference table, ready for data."
        )
    build_games_table(con, snapshots)
    build_drives_table(con, snapshots)
    build_plays_table(con, snapshots)
    build_drive_stats_snapshots_table(con)  # needs games + drives + plays already loaded above
    build_situational_stats_snapshots_table(con)  # needs games + plays already loaded above
    build_advanced_stats_table(con, snapshots)
    build_ppa_snapshots_table(con, snapshots)
    build_sp_ratings_table(con, snapshots)
    build_elo_ratings_table(con, snapshots)
    build_recruiting_table(con, snapshots)
    build_lines_table(con, snapshots)
    build_line_snapshots_table(con)
    build_odds_api_snapshots_table(con)
    normalize_existing_provider_names(con)
    build_venues_table(con)

    con.close()
    print(f"\nDone. DuckDB file at {DB_PATH}")


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
