"""
Phase 3: generates this week's (or any specified week's) matchup projections,
in a layout that pastes straight into the Excel tracker's Weekly Slate tab
(Model Line (Home), Model Total columns).

THE LIVE MODEL IS NOW THE DUB GAMMA MODEL (Sonny-Moore-seeded power rating,
gamma_model.py), per Cole's explicit request to switch the live/actionable
pick over from Ridge. "Model Line (Home)" below -- the ONE column
excel/update_tracker.py reads into Weekly Slate, and the one every downstream
consumer treats as "the pick" -- is now Dub Gamma's spread, not Ridge's.
"Home Win Prob" is ALSO Gamma's own now (gamma_model.predict_home_win_prob(),
using an empirically-fit std -- see gamma_model.gamma_residual_std()'s own
docstring, and Cole's follow-up request quoting Massey's ratings theory page
that this std be "estimated from previous games," not a fixed constant) --
this feeds moneyline grading exactly where Ridge's win prob used to.
Ridge (model.py) is still fit here for its own now-informational "Ridge Line
(Home)"/"Ridge Win Prob" columns -- candidate only, same "never the live
pick" treatment XGBoost's column already had before this change. XGBoost
itself is unaffected -- still a candidate, still written to "XGBoost Line
(Home)".

Ridge is trained on ALL available completed games (no walk-forward cutoff
needed here -- that discipline is for the backtest; a live prediction should
use every game you actually have). Predicts for games in the target week
that haven't been played yet -- same for XGBoost's candidate fit. Dub Gamma
needs no fit at all (see gamma_model.py's own docstring) -- just a full
replay of this season's completed games from the fixed Moore seed, which is
cheap (a few hundred games, one linear pass).

This does add one XGBoost hyperparameter search (RandomizedSearchCV) to
every pipeline run -- unlike model_comparison.py's walk-forward backtest,
which reruns that search once per historical test week (which is why THAT
stays out of run_pipeline.py entirely, see its own docstring), this is a
single fit for the single upcoming week, so the added cost is one search,
not hundreds.

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
import gamma_model
import time
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
    print(f"Trained Ridge on {len(train_df)} completed games. Residual std: {residual_std:.1f} pts. "
          "(Candidate now -- still fit for its Home Win Prob/moneyline use, and its own spread is "
          "written out as an informational column; see this module's docstring.)")

    xgb_pipe, xgb_residual_std = xgboost_model.fit_xgboost_margin_model(train_df)
    print(f"Trained XGBoost candidate on the same {len(train_df)} games. "
          f"Residual std: {xgb_residual_std:.1f} pts. (Informational -- see "
          "excel/update_model_comparison_tab.py's upcoming-games section.)")

    # Dub Gamma Model -- no fitting involved (see gamma_model.py's own
    # docstring), just a full replay of this season's completed games from
    # the fixed Moore seed. Same "informational only" rule as XGBoost above.
    gamma_ratings, gamma_warned = gamma_model.replay_ratings(con)
    if gamma_warned:
        print(f"Dub Gamma: {len(gamma_warned)} team(s) had no Moore seed rating this run, defaulted to "
              f"{gamma_model.DEFAULT_SEED_RATING:.2f} -- see moore_seed_2026.py's MOORE_NAME_ALIASES: "
              f"{gamma_warned}")

    # Gamma's own win probability -- empirically-fit std (Massey's ratings
    # theory: "the standard deviation is estimated from previous games," per
    # Cole's own request), not the old fixed-17.0 constant. See
    # gamma_model.gamma_residual_std()'s own docstring for the walk-forward
    # methodology and the early-season fallback.
    gamma_win_prob_std = gamma_model.gamma_residual_std(con)
    print(f"Dub Gamma win-prob std: {gamma_win_prob_std:.1f} pts "
          f"({'empirically fit' if gamma_win_prob_std != gamma_model.GAMMA_WIN_PROB_STD_FALLBACK else 'fallback -- not enough graded games yet'}).")

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
    # Ridge's own win prob -- informational/candidate only now, same demotion
    # its spread already got (see this module's own docstring). Kept only so
    # the "Ridge Win Prob" column below still exists for comparison.
    ridge_home_win_prob = model.margin_to_home_win_prob(pred_margin, residual_std)

    # predict_margin() is model-agnostic (just pipe.predict(df[FEATURE_COLS])),
    # so it works unchanged on the XGBoost pipeline too -- same reasoning
    # model_comparison.py already relies on to grade both through one code path.
    xgb_pred_margin = model.predict_margin(xgb_pipe, upcoming)
    xgb_spread_home = -xgb_pred_margin

    gamma_spread_home = [
        gamma_model.predict_spread_home(
            gamma_ratings, row.home_team, row.away_team, neutral_site=bool(row.neutral_site)
        )
        for row in upcoming.itertuples()
    ]
    # THE live win prob now -- Gamma's own, via the empirically-fit std
    # computed above (see this module's own docstring / gamma_win_prob_std).
    gamma_home_win_prob = [
        gamma_model.predict_home_win_prob(
            gamma_ratings, row.home_team, row.away_team,
            neutral_site=bool(row.neutral_site), std=gamma_win_prob_std,
        )
        for row in upcoming.itertuples()
    ]

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
        # THE LIVE PICK -- Dub Gamma's spread now, not Ridge's (see this
        # module's own docstring). Weekly Slate (via excel/update_tracker.py)
        # reads exactly this column, unchanged by name, so that script needed
        # no edit at all for this swap to take effect.
        "Model Line (Home)": [round(x, 1) for x in gamma_spread_home],
        "Model Total": [round(x, 1) if x is not None else None for x in model_total],
        # THE live win prob now -- Gamma's own (empirically-fit std, see this
        # module's own docstring). Moneyline grading reads this column,
        # exactly as it read Ridge's before this change.
        "Home Win Prob": [round(x, 3) for x in gamma_home_win_prob],
        # Ridge is now informational only -- see the module docstring. Weekly
        # Slate never reads this column; only
        # excel/update_model_comparison_tab.py does.
        "Ridge Line (Home)": [round(x, 1) for x in model_spread_home],
        # Ridge's own win prob -- informational/candidate only now, same
        # treatment as "Ridge Line (Home)" just above.
        "Ridge Win Prob": [round(x, 3) for x in ridge_home_win_prob],
        # XGBoost stays informational only, same as it always was.
        "XGBoost Line (Home)": [round(x, 1) for x in xgb_spread_home],
    })

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CLEAN_DIR / f"week_{season}_{week}_predictions.csv"
    out.to_csv(out_path, index=False)

    print(f"\n{len(out)} matchups for season {season}, week {week} -- saved to {out_path}\n")
    print(out.to_string(index=False))
    print(
        "\nPaste the 'Model Line (Home)' (Dub Gamma's live pick) and 'Model Total' columns into "
        "the Weekly Slate tab's matching columns (fill in Market Line/Market Total by hand from "
        "your sportsbook). 'Ridge Line (Home)' and 'XGBoost Line (Home)' are informational only -- "
        "run excel/update_model_comparison_tab.py to see them alongside Gamma's line in the Model "
        "Comparison tab's upcoming-games section."
    )

    con.close()


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
