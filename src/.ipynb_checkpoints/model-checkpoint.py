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

A simple pace-based totals baseline is included too (each team's own scoring
average blended with what their opponents have allowed), separate from the
margin model since totals and spreads are different prediction problems.
"""
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import RidgeCV
from sklearn.model_selection import cross_val_predict
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURE_COLS = [
    "rating_diff", "rest_diff", "travel_km_away",
    "elevation_delta_away_ft", "neutral_site_flag", "conference_game_flag",
]
ALPHAS = np.logspace(-2, 3, 25)


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rest_diff"] = df["home_rest_days"] - df["away_rest_days"]
    df["neutral_site_flag"] = df["neutral_site"].astype(float)
    df["conference_game_flag"] = df["conference_game"].astype(float)
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
               m.market_spread_home, m.market_spread_home_open
        FROM game_features f
        JOIN games g ON g.game_id = f.game_id
        LEFT JOIN (
            SELECT game_id,
                   AVG(spread) AS market_spread_home,
                   AVG(spread_open) AS market_spread_home_open
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