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
import time

from config import DB_PATH
from teams import FBS_CONFERENCES

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
               home_team, home_points, away_team, away_points,
               home_conference, away_conference
        FROM games
        WHERE completed = TRUE AND home_points IS NOT NULL AND away_points IS NOT NULL
        ORDER BY start_date, game_id
    """).fetchdf()
    return df


def run_ratings(con):
    """
    Returns (rating_rows, sor_rows). rating_rows feeds ratings_baseline
    exactly as before; sor_rows is Strength of Record, computed in the SAME
    pass over the SAME walk-forward-safe home_before/away_before values, so
    it's free (no extra query, no extra pass over games).

    SOR answers a different question than the Elo rating: not "how good is
    this team," but "how many wins above what an exactly-average team
    (rating == BASE_RATING) would have been expected to earn against this
    exact schedule" -- a resume metric, not a process rating (see PRIME
    CFB's own SOR definition, which this mirrors). Two deliberate design
    choices follow directly from that:
      - It resets to a clean 0.0 at every season boundary, NOT regressed
        75% toward the mean like the Elo rating itself (SEASON_REGRESSION)
        -- a resume should start blank each year, not carry a fraction of
        last year's forward.
      - The "average team" benchmark gets the SAME home-field treatment the
        real team got for that game (home_eff = BASE_RATING + HOME_FIELD_ADV
        when the real team was the home team and the game wasn't at a
        neutral site) -- otherwise a team that played mostly at home would
        look artificially impressive against a benchmark that never got a
        home boost at all.
      - FBS opponents ONLY, same filter (and same reasoning) as
        build_sos_ratings_table()'s own FBS_CONFERENCES check just below --
        crediting/debiting a team's resume for beating an FCS team isn't a
        real answer to "how good is this resume against FBS competition."
        A game against a non-FBS opponent still gets a row (so every
        (game_id, team) a team played has a row to look up, matching
        ratings_baseline's own convention) but sor_after == sor_before,
        i.e. no credit or debit.
    """
    games = load_games(con)
    if games.empty:
        print("power_rating: no completed games in the DB yet (run the pull scripts + build_db.py first)")
        return [], []

    rating = {}          # team -> current rating
    sor = {}             # team -> current Strength of Record (resets to 0.0 each season)
    last_season = {}      # team -> season they last played, for the between-season regression/reset
    rows = []
    sor_rows = []

    for _, g in games.iterrows():
        home, away, season = g["home_team"], g["away_team"], g["season"]

        for team in (home, away):
            if team not in rating:
                rating[team] = BASE_RATING
                sor[team] = 0.0
                last_season[team] = season
            elif last_season[team] < season:
                rating[team] = BASE_RATING + SEASON_REGRESSION * (rating[team] - BASE_RATING)
                sor[team] = 0.0  # clean slate every season -- see docstring above
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
        actual_away = 1.0 - actual_home

        mult = mov_multiplier(margin, home_eff - away_before)
        delta = K_FACTOR * mult * (actual_home - expected_home)

        home_after = home_before + delta
        away_after = away_before - delta

        rating[home], rating[away] = home_after, away_after

        rows.append((g["game_id"], home, True, home_before, home_after))
        rows.append((g["game_id"], away, False, away_before, away_after))

        # --- SOR: an exactly-average team (BASE_RATING) in each real team's
        # shoes, facing the SAME opponent at the SAME rating_before this real
        # game used, with the SAME home-field treatment the real team got.
        home_sor_before, away_sor_before = sor[home], sor[away]
        avg_home_eff = BASE_RATING + (0.0 if g["neutral_site"] else HOME_FIELD_ADV)
        expected_avg_home = expected_score(avg_home_eff, away_before)
        expected_avg_away = expected_score(BASE_RATING, home_before + (0.0 if g["neutral_site"] else HOME_FIELD_ADV))

        home_opp_is_fbs = g["away_conference"] in FBS_CONFERENCES
        away_opp_is_fbs = g["home_conference"] in FBS_CONFERENCES

        home_sor_after = home_sor_before + (actual_home - expected_avg_home) if home_opp_is_fbs else home_sor_before
        away_sor_after = away_sor_before + (actual_away - expected_avg_away) if away_opp_is_fbs else away_sor_before

        sor[home], sor[away] = home_sor_after, away_sor_after
        sor_rows.append((g["game_id"], home, True, home_sor_before, home_sor_after))
        sor_rows.append((g["game_id"], away, False, away_sor_before, away_sor_after))

    return rows, sor_rows


def write_ratings(con, rows):
    con.execute("DELETE FROM ratings_baseline")
    con.executemany("INSERT OR REPLACE INTO ratings_baseline VALUES (?,?,?,?,?)", rows)
    print(f"ratings_baseline: {len(rows)} rows ({len(rows) // 2} games)")


def write_sor(con, rows):
    con.execute("DELETE FROM sor_baseline")
    con.executemany("INSERT OR REPLACE INTO sor_baseline VALUES (?,?,?,?,?)", rows)
    print(f"sor_baseline: {len(rows)} rows ({len(rows) // 2} games)")


def current_ratings(con):
    """Latest rating_after per team -- for projecting games that haven't been played yet."""
    return dict(con.execute("""
        SELECT team, rating_after
        FROM ratings_baseline r
        JOIN games g ON g.game_id = r.game_id
        QUALIFY ROW_NUMBER() OVER (PARTITION BY team ORDER BY g.start_date DESC) = 1
    """).fetchall())


def elo_ratings_through(con, season, through_week):
    """
    Same latest-rating_after-per-team lookup as current_ratings(), but
    walk-forward-scoped: only games strictly before `through_week` of
    `season` (plus every earlier season) count, so a caller doing
    historical walk-forward grading never pulls in a rating move from a
    week that, in real time, hadn't happened yet. Built specifically for
    gamma_model.py's replay_ratings() -- its Elo-calibration step used to
    call current_ratings() unconditionally, which silently leaked future
    weeks (and other teams' later games) into a historical `through_week`
    replay's own placeholder ratings. `through_week` here is exclusive
    (matches "the week whose games haven't been processed yet"), the same
    sense replay_ratings() uses when it computes ratings walking INTO a
    game -- so pass the SAME through_week you'd pass a rating_before
    lookup for that week's own games, not through_week - 1 again.
    """
    return dict(con.execute("""
        SELECT team, rating_after
        FROM ratings_baseline r
        JOIN games g ON g.game_id = r.game_id
        WHERE g.season < ? OR (g.season = ? AND g.week < ?)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY team ORDER BY g.start_date DESC) = 1
    """, [season, season, through_week]).fetchall())


def current_sor(con):
    """
    Latest sor_after per team, for projecting games that haven't been played
    yet -- restricted to the DB's most recent season only (unlike
    current_ratings(), SOR resets every season, so a prior season's final
    value is never a meaningful stand-in for "this team's SOR so far this
    year"; a team with no games yet this season correctly gets no entry
    here, and callers should treat that as 0.0).
    """
    return dict(con.execute("""
        SELECT team, sor_after
        FROM sor_baseline r
        JOIN games g ON g.game_id = r.game_id
        WHERE g.season = (SELECT MAX(season) FROM games)
        QUALIFY ROW_NUMBER() OVER (PARTITION BY team ORDER BY g.start_date DESC) = 1
    """).fetchall())


def build_sos_ratings_table(con):
    """
    Strength of schedule, one row per (season, team) -- see sos_ratings' own
    comment in schema.sql for the full reasoning (why this is computed
    locally instead of pulled from CFBD, why it uses each opponent's
    rating_before rather than their end-of-season rating, and why FCS
    opponents are excluded). Must run AFTER write_ratings() has populated
    ratings_baseline for this run -- it's a self-join against that table.
    """
    df = con.execute("""
        SELECT g.season AS season,
               r_self.team AS team,
               r_opp.rating_before AS opponent_rating_before,
               CASE WHEN r_opp.team = g.home_team THEN g.home_conference ELSE g.away_conference END AS opponent_conference
        FROM ratings_baseline r_self
        JOIN games g ON g.game_id = r_self.game_id
        JOIN ratings_baseline r_opp ON r_opp.game_id = r_self.game_id AND r_opp.team != r_self.team
        WHERE g.season IS NOT NULL
    """).fetchdf()
    if df.empty:
        print("sos_ratings: no ratings_baseline data yet")
        return

    n_before = len(df)
    df = df[df["opponent_conference"].isin(FBS_CONFERENCES)].copy()
    n_dropped = n_before - len(df)
    if n_dropped:
        print(f"sos_ratings: dropped {n_dropped} of {n_before} team-games where the opponent wasn't FBS that season")
    if df.empty:
        print("sos_ratings: no FBS opponents found")
        return

    agg = df.groupby(["season", "team"]).agg(
        avg_opponent_rating=("opponent_rating_before", "mean"),
        n_opponents=("opponent_rating_before", "size"),
    ).reset_index()
    rows = [
        (int(r.season), r.team, float(r.avg_opponent_rating), int(r.n_opponents))
        for r in agg.itertuples()
    ]
    con.execute("DELETE FROM sos_ratings")
    con.executemany(
        "INSERT OR REPLACE INTO sos_ratings (season, team, avg_opponent_rating, n_opponents) VALUES (?,?,?,?)",
        rows,
    )
    print(f"sos_ratings: {len(rows)} rows")


def main():
    con = duckdb.connect(str(DB_PATH))
    rows, sor_rows = run_ratings(con)
    if rows:
        write_ratings(con, rows)
        write_sor(con, sor_rows)
        build_sos_ratings_table(con)
        ranked = sorted(current_ratings(con).items(), key=lambda kv: -kv[1])
        print("\nTop 10 current ratings:")
        for team, r in ranked[:10]:
            print(f"  {r:7.1f}  {team}")
        sor_ranked = sorted(current_sor(con).items(), key=lambda kv: -kv[1])
        print("\nTop 10 current Strength of Record (this season):")
        for team, s in sor_ranked[:10]:
            print(f"  {s:+6.2f}  {team}")
    con.close()


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
