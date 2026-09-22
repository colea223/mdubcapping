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

# A team with ZERO completed games tracked in `games` this season at all --
# NOT the same case DEFAULT_SEED_RATING handles (a real FBS team that's
# played real games this season but has no original Moore seed -- see
# replay_ratings()'s own get_rating() fallback just below). This pipeline
# only pulls FBS + North Dakota State's own FCS history (see config.py's own
# comment on teams.py), so any OTHER FCS opponent in an early-season "money
# game" -- Mercyhurst, Northern Colorado, Cal Poly, Montana State, etc. --
# never appears in `games` at all and so never enters `ratings` through
# replay_ratings(). Confirmed as a real problem once Gamma became the live
# model (per Cole's own testing): defaulting a team like that to
# DEFAULT_SEED_RATING (the flat AVERAGE FBS rating, ~64) made a home FBS
# team look like a coin flip or worse against an opponent a real sportsbook
# would post as a 25-40 point underdog. This is a deliberately low,
# hand-picked placeholder -- NOT derived from any real rating data, just
# "meaningfully below the weakest real FBS team in the Moore seed" -- used
# only in predict_spread_home() below, standing in until every FCS
# opponent's own real strength is tracked here the way North Dakota State's
# already is (see teams.py).
UNRATED_OPPONENT_RATING = 25.0


def true_game_performance_level(net_score, opponent_old_rating, own_injury_weight, opp_injury_weight):
    return net_score + opponent_old_rating + (own_injury_weight - opp_injury_weight)


def update_rating(old_rating, net_score, opponent_old_rating, own_injury_weight=0.0, opp_injury_weight=0.0):
    tgpl = true_game_performance_level(net_score, opponent_old_rating, own_injury_weight, opp_injury_weight)
    return CARRY_FORWARD * old_rating + PERFORMANCE_WEIGHT * tgpl


def predict_spread_home(ratings, home_team, away_team, neutral_site=False):
    """
    home_rating/away_rating fall back to UNRATED_OPPONENT_RATING (NOT
    DEFAULT_SEED_RATING) when a team isn't in `ratings` at all -- see that
    constant's own comment just above for exactly which teams that covers
    and why the two defaults are deliberately different values.
    """
    home_team = normalize_team_name(home_team)
    away_team = normalize_team_name(away_team)
    home_rating = ratings.get(home_team, UNRATED_OPPONENT_RATING)
    away_rating = ratings.get(away_team, UNRATED_OPPONENT_RATING)
    hfa = 0.0 if neutral_site else HFA
    return away_rating - home_rating - hfa


# Fallback standard deviation, used ONLY when there isn't enough graded
# in-season history yet to estimate one empirically (see gamma_residual_std()
# below -- Cole's own follow-up, quoting Massey's own ratings theory page,
# was explicit that "the standard deviation is estimated from previous
# games," not a fixed constant, which supersedes the fixed-17.0-always
# approach this constant used to be). 17.0 points is the standard
# rule-of-thumb value cited for power-rating systems like SP+ -- Cole's
# original source: https://www.reddit.com/r/CFB/comments/dhptj9/
# win_total_probability_distributions_per_sp/ (SP+ win-probability write-up)
# -- kept as the early-season/no-data prior, same role model.py's own
# fit_margin_model() gives its "~14 pts is a reasonable CFB prior" fallback.
GAMMA_WIN_PROB_STD_FALLBACK = 17.0

# Below this many graded (non-seed-week) games this season, gamma_residual_std()
# below just returns GAMMA_WIN_PROB_STD_FALLBACK rather than a same estimate
# off a handful of games -- an empirical std from, say, 3 games is noisier
# than the well-established generic rule-of-thumb value it would replace.
GAMMA_RESIDUAL_STD_MIN_GAMES = 8


def gamma_residual_std(con, season=SEED_SEASON, entering_week=SEED_ENTERING_WEEK, through_week=None,
                        min_games=GAMMA_RESIDUAL_STD_MIN_GAMES, fallback=GAMMA_WIN_PROB_STD_FALLBACK):
    """
    Empirically estimates the standard deviation of (actual scoring margin -
    Gamma's own predicted margin) across this season's completed, graded
    FBS games -- Cole's own requested approach, per Massey's ratings theory
    page: "Probabilities are computed from the ratings by considering the
    predicted margin of victory and assuming a normal distribution of
    possible game results. The standard deviation is estimated from
    previous games." Supersedes the old fixed-17.0-always constant (see
    GAMMA_WIN_PROB_STD_FALLBACK's own comment just above), which is now only
    the early-season fallback below.

    Walk-forward safe, same discipline backtest.py's run_backtest() already
    applies to Gamma's own spread grading: for each week, ratings are
    replayed only through the week BEFORE it (ratings_entering_week()), so
    a game's own residual never uses a rating computed from games that
    happened after it.

    `through_week`, when given, ALSO excludes any game in a week after it --
    e.g. backtest.py passes wk.week - 1 for each historical test week, so
    the std estimate used to grade that week's moneyline picks never uses
    games that (in real time) hadn't been played yet either. None (the
    default, used by predict_week.py/export_site_data.py for live,
    current-week predictions) means "every graded game so far this season."

    Excludes `entering_week` itself (gamma_model.SEED_SEASON's own hindsight
    week -- see moore_seed_2026.py's docstring and backtest.py's
    gamma_is_seed_week comments): that week's "prediction" reuses the Moore
    seed exactly as pasted, which already reflects that week's own results,
    so its residual would be artificially small and skew the estimate.

    Falls back to `fallback` (the fixed SP+/Reddit rule-of-thumb value)
    whenever fewer than `min_games` graded games are available -- an
    empirical std from a handful of games this early in a season is noisier
    than just using the generic value.
    """
    query = """
        SELECT week, home_team, away_team, home_points, away_points, neutral_site
        FROM games
        WHERE season = ? AND week > ? AND completed = TRUE
    """
    params = [season, entering_week]
    if through_week is not None:
        query += " AND week <= ?"
        params.append(through_week)
    query += " ORDER BY week, start_date"
    games = con.execute(query, params).fetchall()
    if not games:
        return fallback

    residuals = []
    ratings_cache = {}
    for week, home_team, away_team, home_points, away_points, neutral_site in games:
        if home_points is None or away_points is None:
            continue
        if week not in ratings_cache:
            ratings_cache[week] = ratings_entering_week(con, season, week)
        wk_ratings = ratings_cache[week]
        pred_spread = predict_spread_home(wk_ratings, home_team, away_team,
                                           neutral_site=bool(neutral_site))
        pred_margin = -pred_spread
        actual_margin = home_points - away_points
        residuals.append(actual_margin - pred_margin)

    if len(residuals) < min_games:
        return fallback

    import numpy as np
    return float(np.std(residuals))


def predict_home_win_prob(ratings, home_team, away_team, neutral_site=False, std=GAMMA_WIN_PROB_STD_FALLBACK):
    """
    Converts Gamma's own predicted spread into a home win probability via a
    normal CDF -- Cole's own requested approach (Massey's ratings theory
    page, see gamma_residual_std()'s own docstring): take the expected
    margin (positive = home favored, the sign flip of predict_spread_home()'s
    own convention), divide by `std`, and run it through the standard normal
    CDF -- P(actual margin > 0) under a Normal(expected margin, std) model.
    Same norm.cdf() building block model.py's own margin_to_home_win_prob()
    uses. Callers should pass `std` from gamma_residual_std(con) (an
    empirically-fit, walk-forward-safe estimate) rather than relying on this
    default -- the default is only here so this function still has a sane
    behavior if called without one.
    """
    from scipy.stats import norm
    spread_home = predict_spread_home(ratings, home_team, away_team, neutral_site=neutral_site)
    predicted_margin_home = -spread_home
    return float(norm.cdf(predicted_margin_home / std))


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

    PER-GAME NORMALIZATION (added after Cole flagged South Carolina QB
    Lanorris Sellers carrying an outsized 24.8 injury weight -- IDENTICAL in
    both week 1 and week 2 -- versus a season-wide median of just 1.13
    across every other team-week). Root cause: total_ppa is a single
    season-cumulative "latest wins" number (see db/schema.sql's
    player_season_ppa comment), not a per-game rate, so a star out for
    multiple straight weeks had his ENTIRE season total re-applied fresh
    every single week he stayed out, rather than a "value of missing him in
    just this one game" estimate -- compounding without bound the longer he
    sat. Dividing by the team's own completed-game count entering that week
    (clamped to at least 1) converts total_ppa into an approximate per-game
    rate that shrinks automatically as more games pass without him, instead
    of reapplying the same full-season number over and over. Team games
    played is used as the proxy denominator (CFBD's season-PPA endpoint
    doesn't expose the player's own games-played), which slightly overstates
    the rate for a player hurt mid-game but is a much closer estimate than
    the un-normalized season total.
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

    # Each team's completed-game weeks this season -- the per-game
    # normalization's denominator. normalize_team_name() here so this joins
    # cleanly against injury_reports/player_season_ppa's own team spellings
    # the same way replay_ratings() already normalizes `games` team names
    # below.
    game_weeks_by_team = {}
    for wk, home_team, away_team in con.execute(
        "SELECT week, home_team, away_team FROM games WHERE season = ? AND completed = TRUE", [season]
    ).fetchall():
        game_weeks_by_team.setdefault(normalize_team_name(home_team), []).append(wk)
        game_weeks_by_team.setdefault(normalize_team_name(away_team), []).append(wk)

    def games_played_before(team, week):
        # Games strictly before `week` -- matches "entering week W" everywhere
        # else in this file (ratings_entering_week()/replay_ratings()).
        weeks = game_weeks_by_team.get(normalize_team_name(team))
        return sum(1 for w in weeks if w < week) if weeks else 0

    weights = {}
    for week, team, last_name, status in injuries:
        if not last_name:
            continue
        player_value = ppa_by_team_lastname.get((team, last_name.strip().lower()))
        if player_value is None or player_value <= 0:
            continue  # unmatched, or a replacement-level/negative season -- contributes nothing either way
        mult, _ = status_weight(status or "")
        games_so_far = max(games_played_before(team, week), 1)
        per_game_value = player_value / games_so_far
        weights[(team, week)] = weights.get((team, week), 0.0) + mult * per_game_value
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
