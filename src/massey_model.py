"""
Massey Rating Method -- a candidate power-rating model, prototyped after
Cole asked "why don't we do the win-loss matrix," comparing Gamma's
sequential exponential-smoothing update (gamma_model.py -- one game at a
time, 90% carry-forward from a fixed Moore seed) against the Sagarin/
Massey/Colley family, which instead solves EVERY team's rating
SIMULTANEOUSLY from the full graph of games played, so every team's
strength is consistent with every other team's at once rather than being
updated one game behind the last.

Candidate only, same as Ridge/XGBoost -- informational, never the live
model. Wired into model_comparison.py so it gets graded head-to-head
against Gamma/Ridge/XGBoost on the exact same walk-forward discipline
before anyone considers promoting it to anything more than that. See that
file's own docstring for how the comparison itself works.

THE MATH (the classic Massey Rating Method, Kenneth Massey 1997 -- the same
family Sagarin's own public ratings and Massey's own public ratings are
built on):

  For every completed FBS game (home vs away):
      adjusted_margin = (home_points - away_points) - HFA
  (HFA is skipped -- adjusted_margin is just the raw margin -- for a
  neutral-site game, same "HFA only ever in a prediction/adjustment, never
  silently assumed for a neutral game" rule gamma_model.py already follows.)

  Massey's insight: for team i, summed across every game team i played, its
  rating minus its opponent's rating should equal that game's
  adjusted_margin, in a least-squares sense. Written as one linear system
  across every team at once:
      M @ r = p
  where M is the "game graph" matrix:
      M[i][i]  = number of games team i has played
      M[i][j]  = -1 * (number of times team i and j played each other)   (i != j)
  and p[i] = the sum of team i's own adjusted_margin across all its games
  (positive when team i outscored its opponents net of HFA, negative when
  it didn't).

  M is singular by construction (every row sums to zero -- ratings are only
  ever defined up to an additive constant, same as every rating system).
  Two things fix that up here:
    1. RIDGE_LAMBDA is added to the diagonal (M += RIDGE_LAMBDA * I). This
       both resolves the singularity AND shrinks every team's rating toward
       a common 0 baseline -- which matters early in a season when the game
       graph is sparse or has disconnected components (an unbeaten
       one-game team could otherwise get an arbitrarily large, essentially
       made-up rating). Real-world Massey/Sagarin-style systems use an
       equivalent regularization, or an assumed-prior-game trick, for
       exactly this reason.
    2. Ratings are re-centered so the pool's own mean is exactly 0 --
       cleans up the arbitrary additive constant regularization leaves
       behind, and keeps a missing/unrated opponent's sane default (0.0,
       "an average team in this pool") meaningful.

  Predicted spread, matching gamma_model.py's own sign convention exactly:
      spread_home = away_rating - home_rating - HFA
  (negative = home favored.)

  KNOWN SIMPLIFICATION worth flagging: a team with ZERO games in the pool
  (e.g. an FCS opponent CFBD never assigns real market lines against
  anyway) defaults to 0.0 -- "an average FBS team" -- rather than
  gamma_model.py's own deliberately-low UNRATED_OPPONENT_RATING treatment
  for the same case. Fine for this comparison (every graded game here has a
  real market line, so a true FCS buy-game essentially never shows up), but
  worth a second look before this model is used for anything that predicts
  those games directly.

Usage (ad hoc -- this module has no CLI of its own beyond the self-test):
    source .venv/bin/activate
    python src/massey_model.py
"""
import time

import numpy as np

from gamma_model import HFA
from teams import normalize_team_name

RIDGE_LAMBDA = 2.0  # regularization strength added to the Massey matrix diagonal -- see module docstring


def fit_massey_ratings(con, season, through_week=None, ridge_lambda=RIDGE_LAMBDA):
    """
    Solves the Massey system from every completed FBS game in `season`
    (optionally restricted to week <= through_week, for walk-forward-safe
    historical grading -- see model_comparison.py's own use of this).
    Returns {team: rating}, re-centered to mean 0 across every team with at
    least one completed game in the pool. Teams with ZERO completed games
    simply aren't in the returned dict at all -- see this module's own
    docstring on why callers default a missing team to 0.0 rather than
    treating a KeyError as a bug.

    `ridge_lambda` is exposed (rather than only ever reading the module
    constant) so self_test() below can dial it to ~0 and check the exact,
    textbook, hand-verifiable solution -- production callers should always
    just use the default.
    """
    query = (
        "SELECT home_team, away_team, home_points, away_points, neutral_site "
        "FROM games WHERE season = ? AND completed = TRUE"
    )
    params = [season]
    if through_week is not None:
        query += " AND week <= ?"
        params.append(through_week)
    rows = con.execute(query, params).fetchall()

    games = []
    teams = set()
    for home_team, away_team, home_points, away_points, neutral_site in rows:
        if home_points is None or away_points is None:
            continue
        home_team = normalize_team_name(home_team)
        away_team = normalize_team_name(away_team)
        hfa = 0.0 if neutral_site else HFA
        adjusted_margin = (home_points - away_points) - hfa
        games.append((home_team, away_team, adjusted_margin))
        teams.add(home_team)
        teams.add(away_team)

    if not games:
        return {}

    team_list = sorted(teams)
    idx = {t: i for i, t in enumerate(team_list)}
    n = len(team_list)

    M = np.zeros((n, n))
    p = np.zeros(n)
    for home_team, away_team, adjusted_margin in games:
        i, j = idx[home_team], idx[away_team]
        M[i, i] += 1
        M[j, j] += 1
        M[i, j] -= 1
        M[j, i] -= 1
        p[i] += adjusted_margin
        p[j] -= adjusted_margin

    M += ridge_lambda * np.eye(n)

    ratings_vec = np.linalg.solve(M, p)
    ratings_vec = ratings_vec - ratings_vec.mean()  # re-center to mean 0, see module docstring

    return {team_list[i]: float(ratings_vec[i]) for i in range(n)}


def ratings_entering_week(con, season, week):
    """Convenience wrapper matching gamma_model.py's own naming/shape --
    the ratings as of just before `week`'s games (i.e. solved through
    week - 1). Cheap to call once per test week: a linear solve over at
    most ~175 FBS teams, not a model fit."""
    return fit_massey_ratings(con, season, through_week=week - 1)


def predict_spread_home(ratings, home_team, away_team, neutral_site=False, default_rating=0.0):
    """Same sign convention as gamma_model.predict_spread_home(): negative
    = home favored. A team missing from `ratings` (zero completed games in
    this pool) falls back to `default_rating` -- 0.0, the pool's own mean
    after re-centering, i.e. "an average team" -- see module docstring for
    the known FCS-buy-game caveat that comes with this choice."""
    home_team = normalize_team_name(home_team)
    away_team = normalize_team_name(away_team)
    home_rating = ratings.get(home_team, default_rating)
    away_rating = ratings.get(away_team, default_rating)
    hfa = 0.0 if neutral_site else HFA
    return away_rating - home_rating - hfa


def self_test():
    """
    Hand-verifiable check on a synthetic 3-team round robin, run every time
    this script is invoked directly -- same "fails loudly and immediately,
    not on a live prediction weeks later" philosophy as gamma_model.py's
    own self_test().

    All games at a neutral site (so HFA drops out of adjusted_margin
    entirely, keeping the arithmetic simple) -- A beats B by 10, B beats C
    by 10, A beats C by only 5 (deliberately NOT the "transitively implied"
    +20, so the system is genuinely over-determined/inconsistent, the way
    real schedules always are, rather than a toy case that happens to solve
    exactly).

    Classic Massey solve (replace one M row with an all-ones row, its own p
    entry with 0, to pin down the additive constant -- textbook, not this
    module's own ridge-regularization path) by hand:
        M' = [[2,-1,-1], [-1,2,-1], [1,1,1]],  p' = [15, 0, 0]
    gives EXACTLY rA=5, rB=0, rC=-5 (checked by substitution in this
    function's own commit -- see the PR/commit message, or just re-derive
    it: row3 forces rC = -rA-rB; substituting into row1 gives 3*rA=15).

    fit_massey_ratings() itself always ridge-regularizes (a real, singular,
    disconnected-early-season graph needs that -- see this module's own
    docstring), so it can't be called with ridge_lambda=0 exactly (M+0*I is
    still singular for this fully-connected-but-rank-deficient example --
    np.linalg.solve would raise). Calling it with a near-zero ridge_lambda
    (1e-9) instead perturbs the exact answer by an amount far below any
    tolerance that matters here, so the assertions below check against the
    hand-derived exact values with a loose-enough tolerance to absorb that
    perturbation, not because the arithmetic is fuzzy.
    """
    import duckdb

    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE games (season INTEGER, week INTEGER, home_team VARCHAR, away_team VARCHAR, "
        "home_points INTEGER, away_points INTEGER, neutral_site BOOLEAN, completed BOOLEAN)"
    )
    con.execute("INSERT INTO games VALUES (9999, 1, 'Team A', 'Team B', 20, 10, TRUE, TRUE)")   # A by 10
    con.execute("INSERT INTO games VALUES (9999, 1, 'Team B', 'Team C', 20, 10, TRUE, TRUE)")   # B by 10
    con.execute("INSERT INTO games VALUES (9999, 1, 'Team A', 'Team C', 15, 10, TRUE, TRUE)")   # A by 5

    ratings = fit_massey_ratings(con, season=9999, ridge_lambda=1e-9)
    assert abs(ratings["Team A"] - 5.0) < 1e-3, ratings
    assert abs(ratings["Team B"] - 0.0) < 1e-3, ratings
    assert abs(ratings["Team C"] - (-5.0)) < 1e-3, ratings

    # Sign convention check, same style as gamma_model.py's own Michigan/OSU
    # worked example -- A (rating 5) at home vs C (rating -5), NOT neutral
    # this time, so HFA subtracts in A's favor: spread_home = -5 - 5 - 4 = -14.
    spread = predict_spread_home(ratings, "Team A", "Team C", neutral_site=False)
    assert abs(spread - (-14.0)) < 1e-3, spread

    con.close()
    print(f"Self-test OK: 3-team round robin -> A=5.0, B=0.0, C=-5.0 "
          f"(ridge_lambda~0); A home vs C spread -> {spread:.1f}")


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    self_test()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
