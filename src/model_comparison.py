"""
Walk-forward comparison of THREE margin-of-victory approaches -- the Dub
Gamma Model (gamma_model.py, a non-fitted model seeded from Sonny Moore's
own ratings, now the LIVE/production model per Cole's explicit request),
Ridge (model.py, a fitted candidate), and XGBoost (xgboost_model.py, a
fitted candidate) -- against each other AND against Vegas's closing spread.
Spread only (not totals/moneyline): both candidates are drop-in alternatives
to Gamma's margin call specifically, and totals_model.py is a separate,
already-validated piece of machinery this comparison doesn't touch.

(Column names below still use the original "ridge"/"xgb"/"gamma" prefixes
from before the live-model swap -- renaming them would break every existing
data/clean/model_comparison_results.csv snapshot and every downstream reader
of this CSV, excel/update_model_comparison_tab.py and export_site_data.py
included, for no real benefit. Read "ridge_*"/"xgb_*" as "the two
candidates" and "gamma_*" as "the live model" throughout this file now.)

Ridge and XGBoost use the same walk-forward discipline as backtest.py: for
every test week, both are refit from scratch using only games whose
start_date is strictly before that week's earliest kickoff, then graded on
that week only, then forgotten. Gamma has no fitting step at all (see
gamma_model.py's own docstring -- it's a deterministic replay from a fixed
seed, not a trained model), but gets the exact same walk-forward-safe
treatment in spirit for gamma_model.SEED_SEASON's week 2 onward:
gamma_model.ratings_entering_week() only ever uses ratings as they stood
BEFORE the test week's games, via the same season/week boundary
Ridge/XGBoost's train/test split already uses.

ONE deliberate exception, per Cole's own request: SEED_SEASON's own week 1
is ALSO graded now, reusing the raw Moore seed exactly as-is (see
run_comparison()'s own comment on gamma_ratings for the mechanics). That
seed was pasted after week 1 had already been played -- see
moore_seed_2026.py's own docstring, it's explicitly "entering week 2" -- so
week 1's grade uses hindsight, not a genuine out-of-sample prediction the
way every other graded week is. Each row carries a gamma_is_seed_week flag
so every downstream consumer (this script's own console summary,
excel/update_model_comparison_tab.py, export_site_data.py, the site) can
label that week as informational rather than presenting it as equivalent to
a real prediction.

Grading itself is backtest.grade_spread_pick() -- the SAME function
backtest.py's own live Gamma grading uses, imported rather than
reimplemented so all three models here are graded through identical rules.
Per Cole's own explicit request, the graded/tracked pick for every model
here is simply whichever side that model's own number favors, full stop --
see backtest.py's own docstring for the full history of that rule (it
previously defaulted a below-threshold pick back to the market's favorite;
that's gone now). is_bet/*_result come out of that shared function too:
*_result is populated for EVERY graded game (not just real, threshold-
clearing bets), so the season-to-date record built from this script's
output reflects the full record, same change as backtest.py.

Still deliberately a standalone script, NOT part of run_pipeline.py itself
(same category backtest.py already established for itself in run_pipeline.py's
own comments, "an evaluation report, not a data step") -- refitting an
XGBoost hyperparameter search for every historical week is meaningfully more
compute than the Ridge-only backtest already does (Gamma adds negligible
cost by comparison -- a plain replay, not a fit), so it stays out of
run_pipeline.py's own STEPS list to keep a local/manual `python
src/run_pipeline.py` run fast. It IS, however, wired directly into
.github/workflows/weekly_pipeline.yml as its own step, immediately after the
full pipeline runs -- Cole asked to stop having to remember to run this by
hand, and that workflow's timeout was raised specifically to give this the
room it needs (see that file's own comment). Still fine to also run by hand
locally (e.g. right after a game you care about finishes) if you want a
fresher read than waiting for the next scheduled run.

Usage:
    source .venv/bin/activate
    python src/model_comparison.py
    python excel/update_model_comparison_tab.py   # writes the result into the tracker
    python src/export_site_data.py                # so the site/Tracking page picks it up too
"""
import time

import duckdb
import numpy as np
import pandas as pd

from config import DB_PATH, CLEAN_DIR
import model
import xgboost_model
import gamma_model
from teams import MW_TEAMS_2026
from backtest import _test_weeks, EDGE_THRESHOLD, MIN_TRAIN_GAMES, grade_spread_pick


def _grade_side(model_spread_home, market_close, actual_margin, edge_threshold):
    """Thin adapter over backtest.grade_spread_pick() -- see this module's
    own docstring for why grading now goes through that shared function
    instead of a local reimplementation. No CLV needed here (this script
    doesn't track it), so market_open is passed as None -- grade_spread_pick()
    already guards CLV on both is_bet AND a real market_open being present."""
    g = grade_spread_pick(model_spread_home, market_close, None, actual_margin, edge_threshold)
    return g["edge"], g["lean"], g["is_bet"], g["bet_result"]


def run_comparison(con, edge_threshold=EDGE_THRESHOLD, min_train_games=MIN_TRAIN_GAMES) -> pd.DataFrame:
    weeks = _test_weeks(con)
    n_weeks = len(weeks)
    full_pool = model.load_training_frame(con).set_index("game_id")

    print(f"Walk-forward comparison: {n_weeks} season/week candidate(s) found. Weeks with fewer "
          f"than {min_train_games} training games so far are skipped (not enough history yet) -- "
          f"grading starts once that's cleared, usually a few weeks into the earliest season.\n")

    results = []
    for idx, wk in enumerate(weeks.itertuples(), start=1):
        train_df = model.load_training_frame(con, before_date=wk.week_start)
        train_df = train_df.dropna(subset=["market_spread_home"])
        if len(train_df) < min_train_games:
            continue

        test_mask = (full_pool["season"] == wk.season) & (full_pool["week"] == wk.week)
        test_df = full_pool[test_mask].dropna(subset=["market_spread_home"])
        if test_df.empty:
            continue

        # Progress output -- this is genuinely the slow part (a full Ridge
        # fit AND an XGBoost hyperparameter search, once per test week), and
        # with nothing printed here a multi-hour run looks indistinguishable
        # from a frozen one. [idx/n_weeks] counts against ALL candidate
        # weeks (including ones just skipped above), so the numbers won't
        # advance one-for-one, but they do climb steadily and confirm real
        # progress -- not just a saved sense of it.
        t0 = time.time()
        print(f"[{idx}/{n_weeks}] {wk.season} week {wk.week}: fitting Ridge + XGBoost on "
              f"{len(train_df)} training games...", flush=True)

        ridge_pipe, ridge_resid = model.fit_margin_model(train_df)
        ridge_pred_margin = model.predict_margin(ridge_pipe, test_df)
        ridge_spread_home = -ridge_pred_margin

        xgb_pipe, xgb_resid = xgboost_model.fit_xgboost_margin_model(train_df)
        xgb_pred_margin = model.predict_margin(xgb_pipe, test_df)
        xgb_spread_home = -xgb_pred_margin

        # No fitting for Gamma (see this module's own docstring) -- just the
        # ratings as they stood entering this test week, via the same
        # con this whole script already has open. Only meaningful for
        # SEED_SEASON (gamma_model.py has no seed for any other season, see
        # moore_seed_2026.py's own docstring) -- other seasons' games simply
        # get gamma_spread_home = None throughout, which _grade_side() below
        # already handles (edge/lean/result all None too).
        #
        # SEED_SEASON's own week 1 (< SEED_ENTERING_WEEK) is a deliberate
        # exception, per Cole's own request: ratings_entering_week() for any
        # week <= SEED_ENTERING_WEEK - 1 already replays zero games (the
        # query's week >= SEED_ENTERING_WEEK AND week <= through_week range
        # is empty), so it harmlessly returns the raw Moore seed unchanged --
        # the exact same "reuse the current seed" grading week 2 already
        # gets. The catch, and why gamma_is_seed_week is threaded through
        # below: that seed was pasted AFTER week 1 was played (see
        # moore_seed_2026.py's own docstring -- it's already "entering week
        # 2"), so grading week 1 with it isn't a genuine out-of-sample
        # prediction, just an informational replay with the answer already
        # baked in. Downstream consumers (export_site_data.py, the site) use
        # gamma_is_seed_week to label this clearly rather than presenting it
        # as equivalent to every other graded week.
        gamma_is_seed_week = (wk.season == gamma_model.SEED_SEASON and wk.week < gamma_model.SEED_ENTERING_WEEK)
        if wk.season == gamma_model.SEED_SEASON:
            gamma_ratings = gamma_model.ratings_entering_week(con, wk.season, wk.week)
        else:
            gamma_ratings = None

        print(f"    done in {time.time() - t0:.0f}s -- grading {len(test_df)} game(s)", flush=True)

        for i, (game_id, row) in enumerate(test_df.iterrows()):
            market_close = row["market_spread_home"]
            actual_margin = row["margin"]

            r_edge, r_lean, r_is_bet, r_result = _grade_side(
                ridge_spread_home[i], market_close, actual_margin, edge_threshold)
            x_edge, x_lean, x_is_bet, x_result = _grade_side(
                xgb_spread_home[i], market_close, actual_margin, edge_threshold)

            if gamma_ratings is not None:
                is_neutral = bool(row["neutral_site"]) if pd.notna(row.get("neutral_site")) else False
                gamma_spread_home_i = gamma_model.predict_spread_home(
                    gamma_ratings, row["home_team"], row["away_team"], neutral_site=is_neutral
                )
                g_edge, g_lean, g_is_bet, g_result = _grade_side(
                    gamma_spread_home_i, market_close, actual_margin, edge_threshold)
            else:
                gamma_spread_home_i = g_edge = g_lean = g_is_bet = g_result = None

            results.append({
                "game_id": game_id, "season": row["season"], "week": row["week"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "home_points": row["home_points"], "away_points": row["away_points"],
                "is_mw_game": row["home_team"] in MW_TEAMS_2026 or row["away_team"] in MW_TEAMS_2026,
                "market_spread_home": market_close, "actual_margin": actual_margin,
                "ridge_spread_home": ridge_spread_home[i], "ridge_edge": r_edge,
                "ridge_lean": r_lean, "ridge_is_bet": r_is_bet, "ridge_result": r_result,
                "xgb_spread_home": xgb_spread_home[i], "xgb_edge": x_edge,
                "xgb_lean": x_lean, "xgb_is_bet": x_is_bet, "xgb_result": x_result,
                "gamma_spread_home": gamma_spread_home_i, "gamma_edge": g_edge,
                "gamma_lean": g_lean, "gamma_is_bet": g_is_bet, "gamma_result": g_result,
                # True only for SEED_SEASON's own week 1 -- see this function's
                # own comment above on gamma_ratings for why that week's grade
                # uses hindsight (the seed already reflects week 1's results)
                # and needs to be labeled as informational, not a real
                # prediction, wherever it's shown.
                "gamma_is_seed_week": bool(gamma_is_seed_week) if gamma_ratings is not None else None,
                # Two candidates agreeing with EACH OTHER -- unaffected by
                # the live-model swap, still just "do Ridge and XGBoost lean
                # the same way."
                "models_agree": (r_lean == x_lean) if (r_lean != "Pick'em" and x_lean != "Pick'em") else None,
                # Each candidate's agreement with the LIVE model (Gamma) --
                # this is the pair the site actually shows as "Agrees with
                # live model" on the Predictions/Results/Tracking pages (see
                # export_site_data.py's _beta_result_dict()/_ridge_result_dict()/
                # build_beta_tracking()/build_ridge_tracking()). Symmetric by
                # construction (order doesn't matter), so the same
                # gamma_agrees_with_ridge column also serves as "does Ridge
                # agree with the live model" from the other direction.
                "gamma_agrees_with_ridge": (
                    (r_lean == g_lean) if (g_lean is not None and r_lean != "Pick'em" and g_lean != "Pick'em")
                    else None
                ),
                "gamma_agrees_with_xgb": (
                    (x_lean == g_lean) if (g_lean is not None and x_lean != "Pick'em" and g_lean != "Pick'em")
                    else None
                ),
            })

    return pd.DataFrame(results)


def summarize(df: pd.DataFrame, prefix: str, label: str) -> dict:
    """
    prefix is 'ridge', 'xgb', or 'gamma' -- picks which model's columns to
    grade. Same "full record is the headline, real-edge broken out
    separately" shape as backtest.py's own summarize() now -- see that
    function's docstring and grade_spread_pick()'s for the full rationale.
    A NaN {prefix}_result (pre-seed Gamma rows, or -- for any prefix -- a
    genuine market-spread-of-0 pick'em with no favorite to default to)
    simply isn't Win/Loss/Push, so it's naturally excluded from both
    n_bets and n_real_edge_bets below without needing its own check.
    """
    if df.empty:
        return {"slice": label, "model": prefix, "n_games": 0}
    result_col, is_bet_col = f"{prefix}_result", f"{prefix}_is_bet"
    bets = df[df[result_col].isin(["Win", "Loss"])]
    wins = (bets[result_col] == "Win").sum()
    losses = (bets[result_col] == "Loss").sum()
    n_bets = wins + losses
    win_rate = wins / n_bets if n_bets else float("nan")
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n_bets if n_bets else float("nan")
    mae = float(np.mean(np.abs(df[f"{prefix}_spread_home"] + df["market_spread_home"])))  # vs. Vegas, informational

    real_edge_bets = df[(df[is_bet_col] == True) & df[result_col].isin(["Win", "Loss"])]
    n_real_edge_bets = int((df[is_bet_col] == True).sum())
    real_edge_wins = int((real_edge_bets[result_col] == "Win").sum())
    real_edge_losses = int((real_edge_bets[result_col] == "Loss").sum())
    n_real_edge_decided = real_edge_wins + real_edge_losses
    real_edge_win_rate = real_edge_wins / n_real_edge_decided if n_real_edge_decided else None

    return {
        "slice": label, "model": prefix, "n_games": len(df), "n_bets": int(n_bets),
        "wins": int(wins), "losses": int(losses),
        "pushes": int((df[result_col] == "Push").sum()),
        "ats_win_rate": round(win_rate, 4) if n_bets else None,
        "roi_flat_stake": round(roi, 4) if n_bets else None,
        "n_real_edge_bets": n_real_edge_bets,
        "real_edge_wins": real_edge_wins, "real_edge_losses": real_edge_losses,
        "real_edge_win_rate": round(real_edge_win_rate, 4) if real_edge_win_rate is not None else None,
    }


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_comparison(con)
    con.close()

    if df.empty:
        print(
            "model_comparison: not enough graded games yet. Same requirement as backtest.py -- "
            f"needs completed games with market lines and at least {MIN_TRAIN_GAMES} of them "
            "before the walk-forward window starts testing."
        )
        return

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CLEAN_DIR / "model_comparison_results.csv"
    df.to_csv(out_path, index=False)
    print(f"Per-game results written to {out_path} ({len(df)} rows)\n")

    agree_rate = df["models_agree"].dropna().mean() if df["models_agree"].notna().any() else None
    if agree_rate is not None:
        print(f"Ridge and XGBoost (the two candidates) agree with EACH OTHER on which side to lean "
              f"in {agree_rate:.1%} of graded games\n")
    ridge_gamma_agree = df["gamma_agrees_with_ridge"].dropna()
    if not ridge_gamma_agree.empty:
        print(f"Ridge agrees with the live model (Gamma) on which side to lean in "
              f"{ridge_gamma_agree.mean():.1%} of graded games (gamma_model.SEED_ENTERING_WEEK onward only)\n")
    if "gamma_agrees_with_xgb" in df.columns:
        xgb_gamma_agree = df["gamma_agrees_with_xgb"].dropna()
        if not xgb_gamma_agree.empty:
            print(f"XGBoost agrees with the live model (Gamma) on which side to lean in "
                  f"{xgb_gamma_agree.mean():.1%} of graded games (gamma_model.SEED_ENTERING_WEEK onward only)\n")

    slices = [("Overall (all FBS)", df), ("Mountain West-involved", df[df["is_mw_game"]])]
    for label, sl in slices:
        print(f"=== {label} ===")
        # Gamma was left out of this loop when it was first added to
        # run_comparison()/the CSV output -- fixed here so the console
        # summary always covers all three models, not just two.
        for prefix, name in [("gamma", "DUB GAMMA (live model)"), ("ridge", "RIDGE (candidate)"),
                              ("xgb", "XGBOOST (candidate)")]:
            s = summarize(sl, prefix, label)
            print(f"--- {name} ---")
            for k, v in s.items():
                if k not in ("slice", "model"):
                    print(f"  {k}: {v}")
        print()


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
