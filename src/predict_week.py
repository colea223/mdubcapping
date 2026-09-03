"""
Phase 3: generates this week's (or any specified week's) matchup projections,
in a layout that pastes straight into the Excel tracker's Weekly Slate tab
(Model Line (Home), Model Total columns).

Trains the margin model on ALL available completed games (no walk-forward
cutoff needed here -- that discipline is for the backtest; a live prediction
should use every game you actually have). Predicts for games in the target
week that haven't been played yet.

Also trains and predicts XGBoost's candidate margin model (xgboost_model.py)
on the exact same training data and the exact same upcoming games, purely so
there's a live, up-to-date XGBoost line to sit next to Ridge's -- same
"informational only, never replacing Ridge" rule everywhere else in this
project applies here too: Weekly Slate (via excel/update_tracker.py) still
only ever reads the "Model Line (Home)"/"Model Total" columns below, which
stay 100% Ridge. The new "XGBoost Line (Home)" column is additional, read
only by excel/update_model_comparison_tab.py's upcoming-games section, so
Ridge remains the one live model driving actual bets. This does add one
XGBoost hyperparameter search (RandomizedSearchCV) to every pipeline run --
unlike model_comparison.py's walk-forward backtest, which reruns that search
once per historical test week (which is why THAT stays out of run_pipeline.py
entirely, see its own docstring), this is a single fit for the single
upcoming week, so the added cost is one search, not hundreds.

Usage:
    source .venv/bin/activate
    python src/predict_week.py                  # auto-detects the next upcoming week
    python src/predict_week.py --season 2026 --week 3
"""
import argparse
from datetime import date

import duckdb
import pandas as pd

from config import DB_PATH, CLEAN_DIR
import model
import totals_model
import xgboost_model
from teams import is_2026_mw_team


def auto_detect_week(con):
    # Only consider incomplete games on or after today -- without this floor,
    # a single stale/bad row (a postponed or data-glitched game sitting in the
    # DB with completed=FALSE from a past season) sorts first by start_date
    # and gets wrongly picked as "next upcoming," even years after it happened.
    today = date.today().isoformat()
    row = con.execute("""
        SELECT season, week, MIN(start_date) AS week_start
        FROM games
        WHERE completed = FALSE AND start_date >= ?
        GROUP BY season, week
        ORDER BY week_start
        LIMIT 1
    """, [today]).fetchone()
    return row  # (season, week, week_start) or None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=None)
    parser.add_argument("--week", type=int, default=None)
    args = parser.parse_args()

    con = duckdb.connect(str(DB_PATH))

    season, week = args.season, args.week
    if season is None or week is None:
        detected = auto_detect_week(con)
        if detected is None:
            print("No upcoming (incomplete) games found in the DB. Run the pull scripts for "
                  "the current season first, or pass --season/--week explicitly.")
            con.close()
            return
        season, week = detected[0], detected[1]
        print(f"Auto-detected next upcoming week: {season} week {week}")

    train_df = model.load_training_frame(con)
    if len(train_df) < 10:
        print(f"Only {len(train_df)} completed games in the DB -- not enough to train on yet. "
              "Run the full pipeline (pull_games/pull_stats/pull_lines/pull_venues/build_db/"
              "power_rating/features) first.")
        con.close()
        return

    pipe, residual_std = model.fit_margin_model(train_df)
    print(f"Trained on {len(train_df)} completed games. Residual std: {residual_std:.1f} pts.")

    xgb_pipe, xgb_residual_std = xgboost_model.fit_xgboost_margin_model(train_df)
    print(f"Trained XGBoost candidate on the same {len(train_df)} games. "
          f"Residual std: {xgb_residual_std:.1f} pts. (Informational -- Ridge above is still "
          "the live model; see excel/update_model_comparison_tab.py's upcoming-games section.)")

    upcoming = model.load_upcoming_frame(con, season, week)
    if upcoming.empty:
        print(f"No games found for season {season}, week {week}.")
        con.close()
        return

    # This project is specifically about Mountain West handicapping -- the
    # model trains on every FBS game for data quality, but the weekly output
    # only needs MW-involved matchups. Narrowing here (rather than in Excel)
    # is what keeps the Weekly Slate tab (41 rows) from silently dropping real
    # MW games behind a flood of national FBS matchups on a busy week.
    total_fbs_games = len(upcoming)
    upcoming = upcoming[
        upcoming["home_team"].apply(is_2026_mw_team) | upcoming["away_team"].apply(is_2026_mw_team)
    ].reset_index(drop=True)
    print(f"{total_fbs_games} FBS games this week nationally -- {len(upcoming)} involve a 2026 Mountain West team.")
    if upcoming.empty:
        print(f"No Mountain West games found for season {season}, week {week}.")
        con.close()
        return

    pred_margin = model.predict_margin(pipe, upcoming)
    model_spread_home = -pred_margin
    home_win_prob = model.margin_to_home_win_prob(pred_margin, residual_std)

    # predict_margin() is model-agnostic (just pipe.predict(df[FEATURE_COLS])),
    # so it works unchanged on the XGBoost pipeline too -- same reasoning
    # model_comparison.py already relies on to grade both through one code path.
    xgb_pred_margin = model.predict_margin(xgb_pipe, upcoming)
    xgb_spread_home = -xgb_pred_margin

    # See src/totals_model.py -- SP+/PPA-based regression, same upgrade the
    # spread model got from the SP+/PPA/talent work, replacing the old
    # raw-scoring-average baseline (model.totals_baseline()).
    totals_train = totals_model.load_totals_training_frame(con)
    total_pipe, _ = totals_model.fit_total_model(totals_train)
    wk_totals_features = totals_model.load_upcoming_totals_frame(con, season, week)
    total_map = {}
    if not wk_totals_features.empty:
        total_preds = totals_model.predict_total(total_pipe, wk_totals_features)
        total_map = dict(zip(wk_totals_features["game_id"].astype(int), total_preds))
    model_total = [total_map.get(int(row.game_id)) for row in upcoming.itertuples()]

    out = pd.DataFrame({
        "Game ID": upcoming["game_id"].values,
        "Week": upcoming["week"].values,
        "Date": pd.to_datetime(upcoming["start_date"]).dt.strftime("%Y-%m-%d"),
        "Away Team": upcoming["away_team"].values,
        "Home Team": upcoming["home_team"].values,
        "Model Line (Home)": [round(x, 1) for x in model_spread_home],
        "Model Total": [round(x, 1) if x is not None else None for x in model_total],
        "Home Win Prob": [round(x, 3) for x in home_win_prob],
        # Informational only -- see the module docstring. Weekly Slate never
        # reads this column; only excel/update_model_comparison_tab.py does.
        "XGBoost Line (Home)": [round(x, 1) for x in xgb_spread_home],
    })

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CLEAN_DIR / f"week_{season}_{week}_predictions.csv"
    out.to_csv(out_path, index=False)

    print(f"\n{len(out)} matchups for season {season}, week {week} -- saved to {out_path}\n")
    print(out.to_string(index=False))
    print(
        "\nPaste the 'Model Line (Home)' and 'Model Total' columns into the Weekly Slate tab's "
        "matching columns (fill in Market Line/Market Total by hand from your sportsbook). "
        "'XGBoost Line (Home)' is informational only -- run excel/update_model_comparison_tab.py "
        "to see it alongside Ridge's line in the Model Comparison tab's upcoming-games section."
    )

    con.close()


if __name__ == "__main__":
    main()
