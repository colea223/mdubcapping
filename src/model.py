"""
Phase 3, Step 2 of the attack plan: a regularized regression on top of the
Phase 2 features, predicting home margin of victory. Deliberately still
simple -- Ridge (via RidgeCV, so alpha is picked by cross-validation rather
than hand-tuned) on a handful of features, not gradient boosting. The plan's
Section 5 is explicit that gradient-boosted trees are an optional, later
step once this simpler model is understood and backtested.

FEATURE_COLS mirrors game_features (src/features.py): rating_diff carries most
of the signal (it's the Elo/Massey baseline from Phase 2); rest/travel/
elevation/neutral/conference are the situational adjustments layered on top.

mw_involved_flag was added after a real backtest showed the model has a
Mountain West-specific home-field bias -- see diagnose_spread_bias.py,
diagnose_spread_home_bias.py, and diagnose_spread_mw_perspective.py for the
full investigation. Short version: there's no explicit home-field-advantage
feature anywhere in this file -- home-field credit is entirely implicit in
the Ridge intercept, which gets calibrated on the whole national, P4-heavy
training set. A real walk-forward backtest showed the model over-predicts
the HOME team's margin specifically whenever a Mountain West team is on the
field, whether MW is hosting (mean residual +1.156 pts) or visiting (the
home/opponent side was over-predicted by a mirror-image +1.128 pts) -- two
almost exactly equal and opposite effects that canceled to +0.037 pts when
MW's own performance was evaluated regardless of home/away role. That
combined number near zero was the key finding: MW teams' overall quality
assessment (via rating_diff/sp_diff/ppa_diff/talent_diff) isn't off, so this
isn't a "the model underrates MW teams" problem -- it's specifically that
whichever side is playing host to (or hosting) a Mountain West opponent gets
too much home-field credit. mw_involved_flag is 1 whenever EITHER team is a
current (2026) Mountain West team, else 0 -- deliberately a single shared
flag rather than separate home/away versions, since the two effects were
almost mirror-image in size and MW-involved games are a small enough slice
(956 of 8433) that a second degree of freedom there is more overfitting risk
than the evidence currently justifies. A negative learned coefficient on
this flag shrinks predicted home-team margin whenever MW is involved, which
is the right correction in BOTH directions: it reduces the over-credited
home margin when MW hosts, and it reduces the over-credited HOST's margin
when MW visits (equivalently: it credits the visiting MW team with the
better-than-expected road performance the backtest actually showed). Uses
team-name membership (MW_TEAMS_2026), not the raw home_conference/
away_conference columns -- those reflect whatever conference a team was
actually in for each historical game (UTEP shows Conference USA pre-2026,
for instance), whereas this needs "is this program a 2026 MW team," the same
convention backtest.py's is_mw_game already uses.

A simple pace-based totals baseline is included too (each team's own scoring
average blended with what their opponents have allowed), separate from the
margin model since totals and spreads are different prediction problems.

drive_yards_diff / drive_points_diff / drive_turnovers_diff / pass_ypd_diff /
rush_ypd_diff / ypa_diff / ypc_diff (added alongside the drive-based-stats +
XGBoost work) are PRIOR-season drive-based rate stats -- see
src/features.py's leakage note and db/schema.sql's drive_stats_snapshots
comment. Same safe-by-construction treatment as sp_diff/ppa_diff/talent_diff:
a whole prior season's aggregate, never this season's own in-progress numbers.

std_down_ppa_diff / passing_down_ppa_diff / red_zone_ppa_diff /
explosive_rate_diff (added alongside the down/distance situational-splits
feature work) are PRIOR-season splits of per-play PPA by standard-down/
passing-down/red-zone situation, plus a prior-season explosive-play rate --
see src/features.py's leakage note and db/schema.sql's
situational_stats_snapshots comment for the exact definitions and the
off-minus-def-allowed netting each diff is built from. Same
safe-by-construction, whole-prior-season treatment as every other *_diff
feature above -- and the attack plan's own top-ranked predictive category
(Section 2/"Recommended Predictive Measures").

WARNING SUPPRESSION: the earliest walk-forward test weeks in backtest.py/
model_comparison.py train on 2016-only games, and every prior-season feature
(sp_diff, ppa_diff, and the 7 drive diffs above) is null for ALL of them --
there's no 2015 data (CFBD pulls start at START_YEAR=2016), so there's
nothing to look up. SimpleImputer(strategy="median") can't compute a median
from zero observed values, so it substitutes 0 and prints a UserWarning
every time -- and since cross-validation/hyperparameter search fits many
separate models per test week (5 CV folds for Ridge, dozens of candidate x
fold combinations for XGBoost's search), that identical, harmless warning
can print hundreds of times over one backtest/comparison run. The filter
below silences ONLY that exact message (matched by its literal prefix, not
a blanket "ignore all UserWarnings") -- it's set at import time specifically
so it also takes effect inside worker processes joblib spawns for
RandomizedSearchCV's n_jobs=-1 search (each spawned worker re-imports this
module fresh, which re-registers the filter there too).
"""
import warnings

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer

warnings.filterwarnings(
    "ignore",
    message="Skipping features without any observed values",
    category=UserWarning,
)
from sklearn.linear_model import RidgeCV
from sklearn.model_selection import cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from teams import MW_TEAMS_2026

FEATURE_COLS = [
    "rating_diff", "rest_diff", "travel_km_away",
    "elevation_delta_away_ft", "neutral_site_flag", "conference_game_flag",
    "sp_diff", "ppa_diff", "talent_diff",
    "mw_involved_flag",
    "drive_yards_diff", "drive_points_diff", "drive_turnovers_diff",
    "pass_ypd_diff", "rush_ypd_diff", "ypa_diff", "ypc_diff",
    # A/B test: temporarily OFF to isolate whether these 4 situational-split
    # features (added alongside the down/distance feature-engineering work)
    # actually improve backtest.py's ROI/ATS/CLV, or just add noise. Re-enable
    # (uncomment) once the comparison is done -- see model.py's own docstring
    # for what each one represents.
    # "std_down_ppa_diff", "passing_down_ppa_diff", "red_zone_ppa_diff", "explosive_rate_diff",
]
ALPHAS = np.logspace(-2, 3, 25)


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rest_diff"] = df["home_rest_days"] - df["away_rest_days"]
    df["neutral_site_flag"] = df["neutral_site"].astype(float)
    df["conference_game_flag"] = df["conference_game"].astype(float)
    df["mw_involved_flag"] = (
        df["home_team"].isin(MW_TEAMS_2026) | df["away_team"].isin(MW_TEAMS_2026)
    ).astype(float)
    return df


def load_training_frame(con, before_date=None) -> pd.DataFrame:
    """
    game_features joined to games (for the actual margin -- only completed
    games have one) and to a per-game consensus market spread. Pass
    before_date (an ISO string) for a walk-forward cutoff: only games that
    started strictly before it are returned.
    """
    query = """
        SELECT f.*, g.start_date, g.home_points, g.away_points,
               m.market_spread_home, m.market_spread_home_open,
               m.market_total, m.market_total_open,
               m.market_home_ml, m.market_away_ml
        FROM game_features f
        JOIN games g ON g.game_id = f.game_id
        LEFT JOIN (
            SELECT game_id,
                   AVG(spread) AS market_spread_home,
                   AVG(spread_open) AS market_spread_home_open,
                   AVG(over_under) AS market_total,
                   AVG(over_under_open) AS market_total_open,
                   AVG(home_moneyline) AS market_home_ml,
                   AVG(away_moneyline) AS market_away_ml
            FROM lines
            GROUP BY game_id
        ) m ON m.game_id = f.game_id
        WHERE g.completed = TRUE AND g.home_points IS NOT NULL AND g.away_points IS NOT NULL
    """
    if before_date:
        query += " AND g.start_date < ?"
        df = con.execute(query, [before_date]).fetchdf()
    else:
        df = con.execute(query).fetchdf()
    df = _prep(df)
    df["margin"] = df["home_points"] - df["away_points"]
    return df


def load_upcoming_frame(con, season: int, week: int) -> pd.DataFrame:
    """game_features for a specific not-yet-played week (no result required)."""
    df = con.execute("""
        SELECT f.*, g.start_date
        FROM game_features f
        JOIN games g ON g.game_id = f.game_id
        WHERE g.season = ? AND g.week = ?
    """, [season, week]).fetchdf()
    return _prep(df)


def build_pipeline() -> Pipeline:
    return Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("scale", StandardScaler()),
        ("ridge", RidgeCV(alphas=ALPHAS)),
    ])


def fit_margin_model(train_df: pd.DataFrame):
    """
    Returns (fitted_pipeline, residual_std). residual_std comes from 5-fold
    out-of-fold cross-validated predictions on the TRAINING set, not in-sample
    residuals -- in-sample residuals understate real error and would make the
    win-probability conversion overconfident.
    """
    X, y = train_df[FEATURE_COLS], train_df["margin"]
    pipe = build_pipeline()
    n_folds = 5 if len(train_df) >= 50 else max(2, min(5, len(train_df) // 10))
    if len(train_df) < 10:
        # Too little data for meaningful CV; fit directly and fall back to
        # in-sample residual std (will be replaced once more data accumulates).
        pipe.fit(X, y)
        resid = y - pipe.predict(X)
    else:
        oof_pred = cross_val_predict(pipe, X, y, cv=n_folds)
        resid = y - oof_pred
        pipe.fit(X, y)  # final model trained on all training data
    residual_std = float(np.std(resid)) if len(resid) else 14.0  # ~14 pts is a reasonable CFB prior
    return pipe, residual_std


def predict_margin(pipe, df: pd.DataFrame) -> np.ndarray:
    return pipe.predict(df[FEATURE_COLS])


def margin_to_home_win_prob(pred_margin: np.ndarray, residual_std: float) -> np.ndarray:
    from scipy.stats import norm
    return norm.cdf(pred_margin / residual_std)


def margin_to_model_spread_home(pred_margin: np.ndarray) -> np.ndarray:
    """Spread convention matches the Excel tracker: negative = home favored."""
    return -pred_margin


# ---------------------------------------------------------------- totals baseline
# SUPERSEDED as of the SP+/PPA totals-model upgrade -- backtest.py and
# predict_week.py both now use src/totals_model.py instead (a regression on
# prior-season SP+/PPA, elevation, and rest, rather than a raw in-season
# scoring average). Left here for reference/comparison; nothing in this
# project calls these two functions anymore. See totals_model.py's docstring
# for why the swap happened -- short version: this baseline had no
# strength-of-schedule adjustment and fell back to a flat league average for
# any team without much in-season history, which a total_edge_threshold
# sensitivity sweep showed was a real (not just noisy) blind spot for MW
# totals specifically.
def totals_baseline(con, before_date=None) -> pd.DataFrame:
    """
    Simple pace baseline: each team's expanding average points scored and
    allowed, computed only from games strictly before the game being
    predicted (same leakage-safety rule as everything else). Predicted total
    for a game = average of (home offense vs away defense) and
    (away offense vs home defense) expected points, summed.
    """
    query = "SELECT game_id, season, week, start_date, home_team, away_team, home_points, away_points FROM games WHERE completed = TRUE"
    if before_date:
        query += " AND start_date < ?"
        games = con.execute(query, [before_date]).fetchdf()
    else:
        games = con.execute(query).fetchdf()
    if games.empty:
        return pd.DataFrame(columns=["game_id", "pred_total"])

    games = games.sort_values("start_date")
    long_rows = []
    for g in games.itertuples():
        long_rows.append({"team": g.home_team, "start_date": g.start_date, "scored": g.home_points, "allowed": g.away_points})
        long_rows.append({"team": g.away_team, "start_date": g.start_date, "scored": g.away_points, "allowed": g.home_points})
    long = pd.DataFrame(long_rows).sort_values(["team", "start_date"])
    long["exp_scored"] = long.groupby("team")["scored"].transform(lambda s: s.expanding().mean().shift(1))
    long["exp_allowed"] = long.groupby("team")["allowed"].transform(lambda s: s.expanding().mean().shift(1))

    league_avg = long["scored"].mean() if not long.empty else 27.0

    team_latest = (
        long.sort_values("start_date").groupby("team")[["exp_scored", "exp_allowed"]].last()
    )
    return team_latest, league_avg


def predict_total_for_matchup(team_latest: pd.DataFrame, league_avg: float, home: str, away: str) -> float:
    def team_row(team):
        if team in team_latest.index:
            row = team_latest.loc[team]
            scored = row["exp_scored"] if pd.notna(row["exp_scored"]) else league_avg
            allowed = row["exp_allowed"] if pd.notna(row["exp_allowed"]) else league_avg
            return scored, allowed
        return league_avg, league_avg

    home_scored, home_allowed = team_row(home)
    away_scored, away_allowed = team_row(away)
    # Each team's expected points = average of their own scoring rate and
    # what the opponent has been allowing.
    home_exp_pts = (home_scored + away_allowed) / 2
    away_exp_pts = (away_scored + home_allowed) / 2
    return home_exp_pts + away_exp_pts
