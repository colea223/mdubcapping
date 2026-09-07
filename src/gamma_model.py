"""
Dub Gamma Model -- a third, deliberately simple power-rating model, seeded
from Sonny Moore's own published ratings (src/moore_seed_2026.py) rather
than fit from CFBD data at all, the way Ridge (model.py) and XGBoost
(xgboost_model.py) are. Informational only, same "never replaces Ridge"
rule as the Dub Beta Model everywhere else in this project -- Weekly Slate
still only ever reads Ridge's own "Model Line (Home)" column.

THE FORMULA (Cole's own description, preserved exactly):

  Prediction for a game, home team vs. away team:
      spread_home = away_rating - home_rating - HFA
  (negative = home favored, matching every other spread_home convention in
  this project -- e.g. Michigan 50 @ Ohio State 55, HFA 4: 50 - 55 - 4 = -9,
  "Ohio State -9").

  After a game finishes, EACH team's rating updates independently:
      net_score = that team's own margin in the game (their points minus
                  the opponent's -- negative for the loser)
      true_game_performance_level (TGPL)
          = net_score
          + opponent's OLD rating (as of just before this game)
          + injury differential (this team's weighted-out value minus the
            opponent's, see load_injury_weights_by_week() below)
      new_rating = 0.9 * old_rating + 0.1 * TGPL

  Worked example straight from Cole (neutral site, so no HFA term in the
  update -- HFA only ever appears in the spread PREDICTION, never in the
  rating update itself): Bears (rating 10, injury level 3.5) beat Vikings
  (rating 4, injury level 1.7) 27-20.
      net_score = +7
      TGPL = 7 + 4 + (3.5 - 1.7) = 12.8
      new_rating = 0.9*10 + 0.1*12.8 = 10.28
  See the bottom of this file (`if __name__ == "__main__"` block calls
  self_test() first) -- it reproduces this exact number before doing
  anything else, so a future edit that breaks the arithmetic fails loudly
  the moment this script is run, not weeks later on a live prediction.

SCOPE: every FBS team's rating self-updates this way (Cole's explicit
choice over "only the 10 MW teams update, opponents stay frozen at their
Moore seed") -- the same "every completed FBS game" scope power_rating.py's
Elo model already uses, so an MW team's opponent-rating input reflects that
opponent's OWN recent form, not a stale preseason number.

WHY THIS IS A FULL REPLAY, NOT A STORED RUNNING VALUE: db/*.duckdb is
gitignored and rebuilt from scratch on every single GitHub Actions run (see
run_pipeline.py's and power_rating.py's own comments for the same
architectural fact) -- there is nowhere to durably store "team X's current
rating" between runs. Instead, every run recomputes the ENTIRE 2026-season
history from the fixed Moore seed (never touched again after the day Cole
pasted it, see moore_seed_2026.py's own docstring) forward through every
completed FBS game since, in chronological order. Cheap (a few hundred
games, one linear pass) and fully deterministic -- exactly the same
"recompute from scratch, never persist mutable state" discipline
power_rating.py's Elo model already established for this project.

INJURY DIFFERENTIAL: the one term with no CFBD equivalent, see
src/pull_injuries.py's own module docstring for where the underlying data
comes from and its real limitations. Weight = that week's reported-out
players' status multiplier (Out=1.0/Doubtful=0.75/Questionable=0.5/
Probable=0.25) times each player's own season-cumulative PPA
(src/pull_stats.py's pull_player_season_ppa), summed per team. Best-effort
by design: a (team, week) with no injury_reports rows at all -- never
scraped that week, or covers.com genuinely listed nothing -- is simply
treated as a 0 injury weight, not an error. This whole term is a refinement
on top of net_score + opponent rating, never something the model can't run
without.

Usage:
    source .venv/bin/activate
    python src/gamma_model.py                # self-test, then current ratings + this week's predictions
"""
import duckdb

from config import DB_PATH
from moore_seed_2026 import SEED_SEASON, SEED_ENTERING_WEEK, SEED_RATINGS, DEFAULT_SEED_RATING
from pull_injuries import status_weight
from teams import normalize_team_name

HFA = 4.0
CARRY_FORWARD = 0.9
PERFORMANCE_WEIGHT = 0.1


def true_game_performance_level(net_score, opponent_old_rating, own_injury_weight, opp_injury_weight):
    return net_score + opponent_old_rating + (own_injury_weight - opp_injury_weight)


def update_rating(old_rating, net_score, opponent_old_rating, own_injury_weight=0.0, opp_injury_weight=0.0):
    tgpl = true_game_performance_level(net_score, opponent_old_rating, own_injury_weight, opp_injury_weight)
    return CARRY_FORWARD * old_rating + PERFORMANCE_WEIGHT * tgpl


def predict_spread_home(ratings, home_team, away_team, neutral_site=False):
    home_team = normalize_team_name(home_team)
    away_team = normalize_team_name(away_team)
    home_rating = ratings.get(home_team, DEFAULT_SEED_RATING)
    away_rating = ratings.get(away_team, DEFAULT_SEED_RATING)
    hfa = 0.0 if neutral_site else HFA
    return away_rating - home_rating - hfa


def load_injury_weights_by_week(con, season):
    """
    Returns {(team, week): total_injury_weight}. Player matching is last
    name + team (covers.com never gives a full first name, see
    pull_injuries.py's own docstring) -- on a last-name collision within the
    same team, the largest-total_ppa candidate wins (the more featured
    player is overwhelmingly the likelier match for a reported injury; a
    true collision between two similarly-featured same-last-name teammates
    is rare enough not to warrant a louder resolution here) rather than
    crashing or silently picking an arbitrary one.

    injury_reports/player_season_ppa are populated by build_db.py from
    db/schema.sql -- on a DB that hasn't had build_db.py run since those
    tables were added, they simply don't exist yet. Same "best-effort,
    never crash the whole model over this" philosophy as a missing week's
    data (see this function's and build_injury_reports_table()'s own
    comments) -- a CatalogException here just means "no injury signal
    available at all yet," not a real error, so this returns {} instead of
    propagating it.
    """
    try:
        injuries = con.execute(
            "SELECT week, team, player_last_name, status FROM injury_reports WHERE season = ?", [season]
        ).fetchall()
    except duckdb.CatalogException:
        return {}
    if not injuries:
        return {}

    ppa_by_team_lastname = {}
    try:
        ppa_rows = con.execute(
            "SELECT player_name, team, total_ppa FROM player_season_ppa WHERE season = ?", [season]
        ).fetchall()
    except duckdb.CatalogException:
        ppa_rows = []
    for name, team, total_ppa in ppa_rows:
        if not name or total_ppa is None:
            continue
        last = name.strip().split(" ")[-1].lower()
        key = (team, last)
        if key not in ppa_by_team_lastname or total_ppa > ppa_by_team_lastname[key]:
            ppa_by_team_lastname[key] = total_ppa

    weights = {}
    for week, team, last_name, status in injuries:
        if not last_name:
            continue
        player_value = ppa_by_team_lastname.get((team, last_name.strip().lower()))
        if player_value is None or player_value <= 0:
            continue  # unmatched, or a replacement-level/negative season -- contributes nothing either way
        mult, _ = status_weight(status or "")
        weights[(team, week)] = weights.get((team, week), 0.0) + mult * player_value
    return weights


def replay_ratings(con, season=SEED_SEASON, entering_week=SEED_ENTERING_WEEK, through_week=None):
    """
    Full chronological replay from the fixed Moore seed through every
    completed FBS game in `season` from `entering_week` on (through
    `through_week` inclusive, or the rest of the season if None). Returns
    (ratings, warned) -- ratings is {team: current_rating} as of the last
    game processed; warned is a sorted list of teams encountered with no
    Moore seed rating (fell back to DEFAULT_SEED_RATING -- see this file's
    and moore_seed_2026.py's own comments on why that's surfaced loudly
    rather than silently absorbed).
    """
    ratings = dict(SEED_RATINGS)
    warned = set()

    def get_rating(team):
        if team not in ratings:
            warned.add(team)
            ratings[team] = DEFAULT_SEED_RATING
        return ratings[team]

    injury_weights = load_injury_weights_by_week(con, season)

    query = """
        SELECT week, home_team, away_team, home_points, away_points
        FROM games
        WHERE season = ? AND week >= ? AND completed = TRUE
    """
    params = [season, entering_week]
    if through_week is not None:
        query += " AND week <= ?"
        params.append(through_week)
    query += " ORDER BY week, start_date"

    for week, home_team, away_team, home_points, away_points in con.execute(query, params).fetchall():
        if home_points is None or away_points is None:
            continue
        home_team = normalize_team_name(home_team)
        away_team = normalize_team_name(away_team)
        old_home, old_away = get_rating(home_team), get_rating(away_team)
        net_home = home_points - away_points
        net_away = -net_home
        home_inj = injury_weights.get((home_team, week), 0.0)
        away_inj = injury_weights.get((away_team, week), 0.0)
        ratings[home_team] = update_rating(old_home, net_home, old_away, home_inj, away_inj)
        ratings[away_team] = update_rating(old_away, net_away, old_home, away_inj, home_inj)

    return ratings, sorted(warned)


def ratings_entering_week(con, season, week):
    """Convenience wrapper for model_comparison.py's walk-forward grading --
    the ratings as of just before `week`'s games (i.e. replayed through
    week-1). Cheap to call once per test week (a plain linear replay over a
    few hundred games, not a model fit) unlike XGBoost's per-week
    hyperparameter search in that same loop."""
    ratings, _warned = replay_ratings(con, season=season, through_week=week - 1)
    return ratings


def self_test():
    """Reproduces Cole's own Bears/Vikings and Michigan/OSU worked examples
    exactly -- run every time this script is invoked directly, so a future
    edit that breaks the arithmetic fails loudly and immediately rather than
    on a live prediction weeks later."""
    bears_new = update_rating(old_rating=10, net_score=7, opponent_old_rating=4,
                               own_injury_weight=3.5, opp_injury_weight=1.7)
    assert abs(bears_new - 10.28) < 1e-9, f"Bears/Vikings self-test failed: got {bears_new}, expected 10.28"

    spread = predict_spread_home({"Ohio State": 55, "Michigan": 50}, home_team="Ohio State", away_team="Michigan")
    assert abs(spread - (-9.0)) < 1e-9, f"Michigan/OSU self-test failed: got {spread}, expected -9.0"

    print("Self-test OK: Bears/Vikings update -> 10.28, Michigan @ Ohio State spread -> -9.0")


def main():
    self_test()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        ratings, warned = replay_ratings(con)
        if warned:
            print(f"\nWARNING: {len(warned)} team(s) had no Moore seed rating, defaulted to "
                  f"{DEFAULT_SEED_RATING:.2f} -- check moore_seed_2026.py's MOORE_NAME_ALIASES: {warned}")

        print(f"\nCurrent Dub Gamma ratings ({len(ratings)} teams):")
        for team in sorted(ratings, key=lambda t: -ratings[t]):
            print(f"  {team:28s} {ratings[team]:7.2f}")

        upcoming = con.execute("""
            SELECT week, home_team, away_team, neutral_site
            FROM games
            WHERE season = ? AND completed = FALSE
            ORDER BY start_date
        """, [SEED_SEASON]).fetchall()
        if not upcoming:
            print("\nNo upcoming games found -- run the pull scripts/build_db.py first.")
            return
        week = upcoming[0][0]
        print(f"\nWeek {week} predictions (Dub Gamma spread_home):")
        for w, home, away, neutral in upcoming:
            if w != week:
                continue
            spread = predict_spread_home(ratings, home, away, neutral_site=bool(neutral))
            print(f"  {away:24s} @ {home:24s}  spread_home={spread:+.1f}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
