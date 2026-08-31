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
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb

from config import RAW_DIR, DB_PATH, END_YEAR

# Line movement is only ever interesting for the season currently being
# played -- a past season's games are over and their lines will never move
# again, so there's no reason to keep backfilling snapshot history for
# them. END_YEAR (config.py) is "the current season" throughout this
# project; only its raw lines_<END_YEAR>_*.json snapshots get scanned here.
LINE_HISTORY_SEASON = END_YEAR
from teams import MW_TEAMS_2026, normalize_team_name

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"

SNAPSHOT_RE = re.compile(r"^(?P<prefix>.+)_(?P<year>\d{4})_(?P<stamp>\d{8}T\d{6}Z)\.json$")


def latest_snapshots():
    """Group raw files by (prefix, year) and keep only the newest stamp for each."""
    best = {}
    for f in RAW_DIR.glob("*.json"):
        m = SNAPSHOT_RE.match(f.name)
        if not m:
            continue
        key = (m.group("prefix"), int(m.group("year")))
        stamp = m.group("stamp")
        if key not in best or stamp > best[key][0]:
            best[key] = (stamp, f)
    return {key: f for key, (stamp, f) in best.items()}


def load_json(path: Path):
    return json.loads(path.read_text())


def all_lines_snapshots():
    """
    Unlike latest_snapshots(), this returns EVERY lines_<year>_<stamp>.json
    ever pulled, not just the newest one -- that full history is exactly what
    the Line History chart needs. Returns a list of (year, stamp, path),
    oldest first.
    """
    out = []
    for f in RAW_DIR.glob("lines_*.json"):
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
    con.executemany("INSERT OR REPLACE INTO advanced_stats VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    print(f"advanced_stats: {len(rows)} rows")


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
    files = sorted(RAW_DIR.glob("venues_static_*.json"))
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
                    line.get("provider"), line.get("spread"), line.get("spread_open"),
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
                    g["id"], line.get("provider"), pulled_at,
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
    files = sorted(RAW_DIR.glob(f"odds_api_{LINE_HISTORY_SEASON}_*.json"))
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
                provider = book.get("title") or book.get("key")
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

    build_teams_table(con)

    snapshots = latest_snapshots()
    if not snapshots:
        print(
            "\nNo raw data pulled yet -- this is expected before you've set a CFBD API "
            "key and run pull_games.py / pull_stats.py / pull_lines.py. The DB now has "
            "its schema and the 2026 team reference table, ready for data."
        )
    build_games_table(con, snapshots)
    build_advanced_stats_table(con, snapshots)
    build_ppa_snapshots_table(con, snapshots)
    build_sp_ratings_table(con, snapshots)
    build_elo_ratings_table(con, snapshots)
    build_recruiting_table(con, snapshots)
    build_lines_table(con, snapshots)
    build_line_snapshots_table(con)
    build_odds_api_snapshots_table(con)
    build_venues_table(con)

    con.close()
    print(f"\nDone. DuckDB file at {DB_PATH}")


if __name__ == "__main__":
    main()
