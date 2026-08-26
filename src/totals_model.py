"""
Regression-based totals model -- the same kind of upgrade the SP+/PPA/talent
work gave the spread model (model.py's FEATURE_COLS), applied to totals
instead. Replaces model.totals_baseline()/predict_total_for_matchup() (the
old pace-average approach) as what backtest.py and predict_week.py actually
use -- those two functions are left in model.py for reference/comparison but
are no longer called anywhere.

Why the old baseline underperformed specifically for Mountain West games: it
was a pure in-season raw scoring average with no strength-of-schedule
adjustment and no prior-season data. That meant any team without much
in-season history yet fell back to a flat league average with zero
team-specific signal -- a real blind spot for a league with an option offense
(Air Force) and altitude venues (Air Force, Wyoming, New Mexico) that a raw
scoring average has no way to represent. A total_edge_threshold sensitivity
sweep run against that baseline (src/sweep_total_threshold.py) confirmed this
wasn't a noise problem: MW ROI stayed deeply negative at every threshold from
1.5 to 5.0 points, while Overall (all FBS) ROI climbed with a higher
threshold as you'd expect from filtering out noise. A model that's
systematically wrong doesn't get fixed by being more selective about when to
trust it.

This version predicts TOTAL points (home_points + away_points) with a Ridge
regression (RidgeCV, same as model.py, so alpha is cross-validated rather
than hand-tuned) over:
  - prior-season SP+ offense_rating / defense_rating for both teams. These
    are season-end aggregates in CFBD, so PRIOR season only -- same
    walk-forward-safe rule as sp_diff/ppa_diff in features.py. They're
    already on a points-scale (league-average offense_rating is ~27, i.e.
    roughly a typical team's points per game) rather than an
    above/below-average scale, which is exactly what makes SUM meaningful
    here: home_sp_off + away_sp_off is a genuine "how much scoring talent is
    on the field" signal. Contrast with game_features' sp_diff, which is a
    DIFFERENCE (home minus away) -- exactly right for predicting who wins by
    how much, but it throws away the level information a total needs (two
    elite offenses playing each other has sp_diff near zero but should
    still produce a high total).
  - prior-season PPA (offensive/defensive), same sum-not-diff logic.
  - IN-season PPA (offensive/defensive), same sum-not-diff logic again, but
    from ppa_snapshots instead of advanced_stats -- this season's own
    opponent-adjusted efficiency numbers, computed only from games through
    the week strictly before the one being predicted (see the ASOF join
    below and the long comment on ppa_snapshots in schema.sql). This is the
    feature under test in this version. Two things tried before this one
    both failed real backtests despite looking fine on raw accuracy, and the
    reasoning here is worth being explicit about since it's the same
    "in-season" territory:
      - THIS-season raw scoring form (home_form_scored/away_form_scored,
        expanding averages of actual points) made ROI worse everywhere.
        Likely cause: a team's raw scoring trend is about as public and
        fast-moving a signal as exists -- a sportsbook's own total is
        already reacting to it, so a model that converges toward it mostly
        converges toward the market, not toward genuine edge.
      - Prior-season pace/tempo (plays run per game, LAST season) also made
        both MAE and ROI worse. Likely cause: for any team with a thin
        prior-season game count (a true newcomer, or a team that changed
        divisions), plays-per-game is a noisy ratio over a small
        denominator, and that noise got amplified through standardization.
    In-season PPA is a different bet than either: it's opponent-adjusted and
    garbage-time-excluded (not a raw scoring number a book is trivially
    tracking), and it isn't a plays-per-game ratio over a thin sample (it's
    CFBD's own play-level aggregation, already regularized by however CFBD
    computes it). Whether that's enough to survive real backtesting the way
    the first two didn't is exactly what this version is testing -- it isn't
    assumed here.
  - elevation and rest days, reused as-is from game_features -- these are
    static per-game metadata (not derived from any game outcome), so
    there's no leakage concern borrowing them from the spread model's
    feature table instead of recomputing them here.
  - neutral_site / conference_game flags.

Why in-season SP+ (the other half of "in-season SP+/PPA") isn't here: CFBD's
SP+ endpoint (RatingsApi.get_sp) takes only `year`, no week parameter -- there
is no way to reconstruct "what was SP+ as of week 6 of 2019" retroactively,
which means an in-season SP+ feature could only ever be validated by waiting
through real live weeks after shipping it, not backtested against history
first the way everything else in this file has been. That's a materially
different risk profile from in-season PPA (whose week-level history CAN be
reconstructed via end_week -- see backfill_ppa_snapshots.py), so the two were
deliberately split into separate candidates rather than bundled, and PPA goes
first because it's the one that can be honestly tested before being trusted.

Same walk-forward discipline as everywhere else in this project: fit only on
games strictly before the one being predicted, forgotten and refit for the
next test week (backtest.py) or trained on everything available for a live
prediction (predict_week.py, same convention model.py itself uses there).

mw_involved_flag was added after a real backtest showed this model has a
Mountain West-specific scoring bias -- see diagnose_totals_bias.py for the
full investigation (built as the totals equivalent of the spread model's
diagnose_spread_bias.py/diagnose_spread_home_bias.py, once the spread fix's
success made "check totals for the same shape of problem" worth doing).
Short version: the raw residual (model_total - actual_total, independent of
the market) was +0.144 pts overall -- statistically indistinguishable from
zero given the scale of scoring variance here -- but +1.552 pts for
Mountain West games specifically, a real, non-noise signal. That showed up
directly in the betting numbers: the model leans Over 61.3% of all games but
67.0% of MW games (50% would mean no skew), and Over bets underperform Under
bets in both slices (worst for MW, where Under leans were the only
profitable slice found: +1.69% ROI vs Over's -3.80%). Altitude was checked
directly the same way the spread investigation did and again didn't hold up
-- elevation/residual correlation was ~0 in both slices, no clean trend
across elevation buckets, and New Mexico (one of the three altitude-hosting
teams) actually showed a NEGATIVE residual. The per-team breakdown showed
the bias broadly shared (8 of 10 MW teams positive) rather than one team's
data problem, but Wyoming (+6.05 pts) and Air Force (+4.41 pts) stood out
well above the rest -- both clock-control, low-possession offensive styles
(Wyoming's ground game, Air Force's triple option) that suppress total
plays run in a game for BOTH teams, a different mechanism than the generic
plays-per-game pace feature already tried and reverted (that one measured
pace uniformly across every team; this is about a stylistic subset the
model has no explicit way to represent). mw_involved_flag is 1 whenever
EITHER team is a current (2026) Mountain West team, else 0 -- the same
single-shared-flag choice model.py made for the spread fix, for the same
reason: MW-involved games are a small enough slice (956 of 8433) that a
team-by-team correction is more overfitting risk than the evidence
currently supports, and a uniform flag at least captures the broadly-shared
direction (8 of 10 teams positive) even though it won't fully correct
Wyoming/Air Force's larger individual effect. A negative learned
coefficient on this flag shrinks predicted total whenever MW is involved,
which is the right direction given the model over-predicts MW scoring.
"""
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import RidgeCV
from sklearn.model_selection import cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from teams import MW_TEAMS_2026

FEATURE_COLS = [
    "sp_off_sum", "sp_def_sum", "ppa_off_sum", "ppa_def_sum",
    "in_ppa_off_sum", "in_ppa_def_sum",
    "home_rest_days", "away_rest_days", "travel_km_away",
    "elevation_delta_away_ft", "neutral_site_flag", "conference_game_flag",
    "mw_involved_flag",
]
ALPHAS = np.logspace(-2, 3, 25)

# ppa_h/ppa_a: ppa_snapshots rows for the home/away team, matched via ASOF
# JOIN (DuckDB's "nearest match at or before" join) on as_of_week <= week - 1
# -- strictly the week BEFORE the one being predicted, so even a game in
# week W can't see week W's own numbers (some week-W games kick off before
# others in the same week). A team with no games yet this season (week 1,
# or a bye-heavy start) simply gets no match -- NULL, imputed away by the
# pipeline exactly like missing prior-season SP+/PPA already is, and it
# fills in as the season goes on. See schema.sql's ppa_snapshots comment and
# backfill_ppa_snapshots.py / pull_stats.py for how that table gets built
# and kept current.
_JOIN_SQL = """
    JOIN game_features f ON f.game_id = g.game_id
    LEFT JOIN sp_ratings sp_h ON sp_h.season = g.season - 1 AND sp_h.team = g.home_team
    LEFT JOIN sp_ratings sp_a ON sp_a.season = g.season - 1 AND sp_a.team = g.away_team
    LEFT JOIN advanced_stats adv_h ON adv_h.season = g.season - 1 AND adv_h.team = g.home_team
    LEFT JOIN advanced_stats adv_a ON adv_a.season = g.season - 1 AND adv_a.team = g.away_team
    ASOF LEFT JOIN ppa_snapshots ppa_h ON ppa_h.team = g.home_team AND ppa_h.season = g.season AND ppa_h.as_of_week <= g.week - 1
    ASOF LEFT JOIN ppa_snapshots ppa_a ON ppa_a.team = g.away_team AND ppa_a.season = g.season AND ppa_a.as_of_week <= g.week - 1
"""
_SELECT_COLS = """
    g.game_id, g.season, g.week, g.start_date, g.home_team, g.away_team,
    f.home_rest_days, f.away_rest_days, f.travel_km_away,
    f.elevation_delta_away_ft, f.neutral_site, f.conference_game,
    sp_h.offense_rating AS home_sp_off, sp_h.defense_rating AS home_sp_def,
    sp_a.offense_rating AS away_sp_off, sp_a.defense_rating AS away_sp_def,
    adv_h.off_ppa AS home_ppa_off, adv_h.def_ppa AS home_ppa_def,
    adv_a.off_ppa AS away_ppa_off, adv_a.def_ppa AS away_ppa_def,
    ppa_h.off_ppa AS home_in_ppa_off, ppa_h.def_ppa AS home_in_ppa_def,
    ppa_a.off_ppa AS away_in_ppa_off, ppa_a.def_ppa AS away_in_ppa_def
"""


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["neutral_site_flag"] = df["neutral_site"].astype(float)
    df["conference_game_flag"] = df["conference_game"].astype(float)
    df["sp_off_sum"] = df["home_sp_off"] + df["away_sp_off"]
    df["sp_def_sum"] = df["home_sp_def"] + df["away_sp_def"]
    df["ppa_off_sum"] = df["home_ppa_off"] + df["away_ppa_off"]
    df["ppa_def_sum"] = df["home_ppa_def"] + df["away_ppa_def"]
    # Same SUM-not-DIFFERENCE logic as sp_off_sum/ppa_off_sum above, just
    # sourced from this-season-so-far numbers instead of last season's.
    # Deliberately kept as its OWN pair of features rather than blended into
    # or replacing the prior-season ppa_off_sum/ppa_def_sum -- early in a
    # season this is NaN/imputed for every team (nothing to average yet) and
    # the prior-season features carry the load, same as they always have;
    # the regression itself learns how much to lean on each as real
    # in-season data accumulates, rather than that handoff being hand-coded.
    df["in_ppa_off_sum"] = df["home_in_ppa_off"] + df["away_in_ppa_off"]
    df["in_ppa_def_sum"] = df["home_in_ppa_def"] + df["away_in_ppa_def"]
    df["mw_involved_flag"] = (
        df["home_team"].isin(MW_TEAMS_2026) | df["away_team"].isin(MW_TEAMS_2026)
    ).astype(float)
    return df


def load_totals_training_frame(con, before_date=None) -> pd.DataFrame:
    """
    Completed games with a known actual_total, joined to prior-season SP+/PPA,
    in-season PPA, and game_features for elevation/rest/travel/neutral/
    conference. Pass before_date (an ISO string) for a walk-forward cutoff:
    only games that started strictly before it are returned -- same
    convention as model.load_training_frame().
    """
    query = f"""
        SELECT {_SELECT_COLS}, g.home_points, g.away_points
        FROM games g
        {_JOIN_SQL}
        WHERE g.completed = TRUE AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
    """
    if before_date:
        query += " AND g.start_date < ?"
        df = con.execute(query, [before_date]).fetchdf()
    else:
        df = con.execute(query).fetchdf()
    df = _prep(df)
    df["actual_total"] = df["home_points"] + df["away_points"]
    return df


def load_upcoming_totals_frame(con, season: int, week: int) -> pd.DataFrame:
    """Same shape as load_totals_training_frame() but for a specific week, played or not."""
    query = f"""
        SELECT {_SELECT_COLS}
        FROM games g
        {_JOIN_SQL}
        WHERE g.season = ? AND g.week = ?
    """
    df = con.execute(query, [season, week]).fetchdf()
    return _prep(df)


def build_pipeline() -> Pipeline:
    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("ridge", RidgeCV(alphas=ALPHAS)),
    ])


def fit_total_model(train_df: pd.DataFrame):
    """
    Returns (fitted_pipeline, residual_std), same cross-validated-residual
    discipline as model.fit_margin_model() -- residual_std comes from 5-fold
    out-of-fold predictions on the training set, not in-sample residuals.
    """
    X, y = train_df[FEATURE_COLS], train_df["actual_total"]
    pipe = build_pipeline()
    n_folds = 5 if len(train_df) >= 50 else max(2, min(5, len(train_df) // 10))
    if len(train_df) < 10:
        pipe.fit(X, y)
        resid = y - pipe.predict(X)
    else:
        oof_pred = cross_val_predict(pipe, X, y, cv=n_folds)
        resid = y - oof_pred
        pipe.fit(X, y)  # final model trained on all training data
    residual_std = float(np.std(resid)) if len(resid) else 13.0  # ~13 pts is a reasonable CFB total prior
    return pipe, residual_std


def predict_total(pipe, df: pd.DataFrame) -> np.ndarray:
    return pipe.predict(df[FEATURE_COLS])
