"""
Phase 3, Section 6 of the attack plan: a walk-forward backtest of the model
against the market -- spread, total (over/under), AND moneyline, each graded
with real units at real odds (moneyline uses the actual home/away American
odds from that game, not an assumed price; spread and total use -110, the
standard price absent a book-specific number in CFBD's lines data).

The one non-negotiable rule from the plan: never fit or tune using data from
after the game being predicted. This script enforces that literally -- for
every test week, the model (and the totals baseline) is rebuilt from scratch
using only games whose start_date is strictly before that week's earliest
kickoff, and it forgets that fit before moving to the next week. This is
expanding-window walk-forward, so early seasons are pure training data and
the harness only starts grading once there's a reasonable amount of history
behind it.

Metrics computed, per the plan, for EACH of spread/total/moneyline:
  - Win rate (spread/total need ~52.4% at -110 to break even; moneyline's
    break-even rate depends on the odds actually taken, so ROI is the number
    that actually matters there)
  - CLV (closing line value) for spread/total -- did the number move in your
    favor by closing? Positive = yes. Skipped for moneyline: CFBD's lines
    table only stores one moneyline snapshot per game, not an opening price,
    so there's nothing to compare against.
  - Calibration (Brier score) -- do stated win probabilities match reality?
    (shared across bet types since it's about the model's win-prob estimate)
  - ROI at flat 1-unit stake -- -110 for spread/total, real odds for moneyline
  - All of the above sliced overall AND filtered to games involving a 2026
    Mountain West team, since the whole point is MW-specific edges.

EVERY GRADED GAME COUNTS TOWARD THE RECORD NOW (spread + total; moneyline
unchanged) -- previously, a game whose edge didn't clear EDGE_THRESHOLD was
simply left out of the record entirely (no lean/no bet at all). Per Cole's
own request, that's gone: every game with a market line now gets graded,
so the season record reflects "if you always took a side on every game,"
not just the subset where the model found a real edge.

SPREAD LEAN IS ALWAYS THE MODEL'S OWN RAW SIDE (updated per a later request
from Cole -- this superseded an earlier version of this rule). grade_spread_
pick() used to default back to the market's own favorite whenever the edge
was too small to clear EDGE_THRESHOLD, on the theory that a tiny edge can
point either direction almost at random. Cole explicitly asked to drop that
override: the graded/tracked pick is now simply whichever side the model's
own number favors, full stop, even when the edge is small. EDGE_THRESHOLD
hasn't gone away -- is_bet still flags whether a given pick cleared it, so
"how'd the model do overall" and "how'd the model do on its real, high-
confidence edges specifically" (n_real_edge_bets/real_edge_win_rate) stay
separately reportable -- it just no longer changes WHICH side gets graded.
  - Total: no change from the original "every graded game counts" rollout --
    total_lean was ALREADY computed as "whichever side the model's own
    number leans toward" regardless of the edge threshold, so there was
    never a separate "favorite" concept to substitute in for totals.
  is_bet/is_total_bet keep their EXACT old meaning -- "a real, threshold-
  clearing edge you'd actually stake money on" -- and still gate CLV (there's
  no real closing-line comparison to make on a game you never actually
  bet). summarize()/summarize_totals() report the full record as the
  headline numbers now, with n_real_edge_bets/etc. as a secondary
  breakdown of how many of those were genuine value plays vs. just
  whichever way the model's raw number happened to lean.

THE LIVE MODEL'S SPREAD PICK IS NOW THE DUB GAMMA MODEL (updated per Cole's
explicit request to switch the live/actionable pick over from Ridge to the
Sonny-Moore-seeded power rating in gamma_model.py -- "I want the moore model
to be the live model"). run_backtest() below now grades SPREAD the exact
same way model_comparison.py already grades Gamma's own column: per test
week, gamma_model.ratings_entering_week() gives the ratings as they stood
BEFORE that week's games (walk-forward safe, same discipline as Ridge's own
per-week refit), and gamma_model.predict_spread_home() turns that into a
spread for grading via the SAME shared grade_spread_pick() every other model
in this project uses.

Gamma has no rating (and so no spread prediction at all) for any season
before gamma_model.SEED_SEASON -- see moore_seed_2026.py's own docstring --
so every pre-SEED_SEASON test week here simply has no spread pick (edge/
lean/is_bet/bet_result/clv/model_spread_home all None for those rows). This
doesn't affect the site's Tracking/Results pages: they already filter this
DataFrame down to CURRENT_SEASON == gamma_model.SEED_SEASON before doing
anything with it. Running `python src/backtest.py` directly, though, will
now show a smaller SPREAD- and MONEYLINE-specific overall record than TOTAL
does (TOTAL is untouched by this change -- still comes from
totals_model.py's regression). That's an accepted, documented trade-off of
Gamma's seed having no pre-2026 history, not a bug.

MONEYLINE IS ALSO GAMMA'S NOW (Cole's own follow-up request, quoting
Massey's ratings theory page: win probabilities are "computed from the
ratings by considering the predicted margin of victory and assuming a
normal distribution of possible game results," with "the standard deviation
... estimated from previous games"). gamma_model.predict_home_win_prob()
converts Gamma's own predicted margin into a win probability via a normal
CDF, using gamma_model.gamma_residual_std() for the std -- computed
walk-forward per test week here (through_week=wk.week - 1, so a week's own
moneyline grading never uses a std estimated off games that, in real time,
hadn't been played yet). Same "None for any pre-SEED_SEASON game" treatment
as the spread pick above.

gamma_is_seed_week (added per-row, mirroring model_comparison.py exactly):
True only for gamma_model.SEED_SEASON's own week 1, which reuses the raw
Moore seed exactly as pasted -- and that seed was pasted AFTER week 1 was
already played (see moore_seed_2026.py's own docstring, it's explicitly
"entering week 2"), so grading week 1 with it is hindsight, not a genuine
out-of-sample prediction the way every other graded week is. Downstream
consumers (export_site_data.py, the site) use this flag to label that week
clearly rather than presenting it as equivalent to a real prediction.

Ridge (model.py) is STILL fit here, every test week, same walk-forward
discipline as always -- it's no longer used for the spread pick OR
moneyline; its win-probability output (ridge_home_win_prob) is now purely
an informational/candidate column, same demotion its spread already got.

Usage:
    source .venv/bin/activate
    python src/backtest.py
"""
import duckdb
import numpy as np
import pandas as pd

from config import DB_PATH, CLEAN_DIR
import model
import totals_model
import gamma_model
import time
from odds import no_vig_prob, payout_profit
from teams import MW_TEAMS_2026

EDGE_THRESHOLD = 2.0        # points -- matches the Excel tracker's Settings default (spread AND total)
ML_EDGE_THRESHOLD = 0.05    # model win prob vs. no-vig market prob, in probability points
MIN_TRAIN_GAMES = 100    # roughly two synthetic/actual seasons before grading starts


def _test_weeks(con):
    return con.execute("""
        SELECT season, week, MIN(start_date) AS week_start
        FROM games
        WHERE completed = TRUE
        GROUP BY season, week
        ORDER BY week_start
    """).fetchdf()


def grade_spread_pick(model_spread_home, market_close, market_open, actual_margin, edge_threshold):
    """
    Shared spread-grading logic -- factored out so backtest.py's own live
    Ridge grading and model_comparison.py's Ridge/XGBoost/Gamma 3-way
    comparison always apply the EXACT same favorite-default rule (see this
    module's own docstring for the full rationale). Returns a dict:
    edge, raw_lean (the model's own value-sign lean, pre-override),
    favorite (the market's favorite side, or None for a true pick'em),
    is_bet (a real, threshold-clearing edge), lean (the actual tracked
    pick -- raw_lean if is_bet, else favorite), bet_result (Win/Loss/Push,
    or None only for a true pick'em with no favorite to default to), and
    clv_pts (only ever populated when is_bet -- there's no closing number
    to compare against on a game that was never actually bet).
    """
    edge = market_close - model_spread_home
    raw_lean = "Home" if edge > 0 else ("Away" if edge < 0 else "Pick'em")

    if market_close < 0:
        favorite = "Home"
    elif market_close > 0:
        favorite = "Away"
    else:
        favorite = None

    # bool(...) wrapper is load-bearing, not defensive style: model_spread_home/
    # market_close often trace back to a numpy array element (a model's
    # array output), so `abs(edge) >= edge_threshold` is numpy.bool_, and
    # `numpy.bool_ and <python bool>` can itself evaluate to numpy.bool_
    # depending on which side short-circuits -- which json.dumps() can't
    # serialize (see export_site_data.py's own comments on this exact
    # gotcha, hit once already this project). Normalizing here means every
    # caller of this function gets a real Python bool for free.
    is_bet = bool(abs(edge) >= edge_threshold and raw_lean != "Pick'em")

    # Per Cole's own explicit request, the tracked/graded pick is ALWAYS the
    # model's own raw lean now -- never overridden back to the market's
    # favorite just because the edge is small. `is_bet` (the edge_threshold
    # check above) still exists and is still returned below -- it's kept
    # purely as a "was this a real, high-confidence edge" label for
    # reporting (n_real_edge_bets/real_edge_win_rate and similar breakdowns
    # downstream), it just no longer changes WHICH side gets graded.
    # `favorite` is likewise kept in the return value for anything downstream
    # that still wants to know which side the market favored, but nothing
    # here uses it to pick a side anymore.
    lean = raw_lean

    cover_value = actual_margin + market_close
    if cover_value > 0:
        home_covers = True
    elif cover_value < 0:
        home_covers = False
    else:
        home_covers = None  # push

    bet_result = None
    clv_pts = None
    if lean != "Pick'em":
        if home_covers is None:
            bet_result = "Push"
        elif (lean == "Home") == home_covers:
            bet_result = "Win"
        else:
            bet_result = "Loss"
        if is_bet and market_open is not None and pd.notna(market_open):
            clv_pts = (market_open - market_close) if lean == "Home" else (market_close - market_open)

    return {
        "edge": edge, "raw_lean": raw_lean, "favorite": favorite,
        "is_bet": is_bet, "lean": lean, "bet_result": bet_result, "clv_pts": clv_pts,
    }


def run_backtest(con, edge_threshold=EDGE_THRESHOLD, ml_edge_threshold=ML_EDGE_THRESHOLD,
                  min_train_games=MIN_TRAIN_GAMES) -> pd.DataFrame:
    weeks = _test_weeks(con)
    full_pool = model.load_training_frame(con)  # every completed game w/ a market line, for slicing test rows out of
    full_pool = full_pool.set_index("game_id")

    results = []
    for wk in weeks.itertuples():
        train_df = model.load_training_frame(con, before_date=wk.week_start)
        train_df = train_df.dropna(subset=["market_spread_home"])
        if len(train_df) < min_train_games:
            continue

        test_mask = (full_pool["season"] == wk.season) & (full_pool["week"] == wk.week)
        test_df = full_pool[test_mask].dropna(subset=["market_spread_home"])
        if test_df.empty:
            continue

        # Ridge -- still fit every test week, same walk-forward discipline as
        # always, but now used ONLY for its own informational candidate win
        # prob (ridge_home_win_prob below) -- see this module's own docstring
        # for why Gamma took over both the spread pick AND moneyline below.
        pipe, residual_std = model.fit_margin_model(train_df)
        pred_margin = model.predict_margin(pipe, test_df)
        ridge_home_win_prob = model.margin_to_home_win_prob(pred_margin, residual_std)

        # Dub Gamma Model -- THE live spread pick now (see module docstring).
        # No fitting involved (gamma_model.py is a deterministic replay, not
        # a trained model) -- just the ratings as they stood entering this
        # test week, exactly like model_comparison.py's own walk-forward
        # Gamma grading. Only meaningful for gamma_model.SEED_SEASON (no seed
        # exists for any other season) -- other seasons get gamma_ratings =
        # None, and every game that test week simply has no spread pick at
        # all (see this module's own docstring).
        gamma_is_seed_week = (wk.season == gamma_model.SEED_SEASON and wk.week < gamma_model.SEED_ENTERING_WEEK)
        if wk.season == gamma_model.SEED_SEASON:
            gamma_ratings = gamma_model.ratings_entering_week(con, wk.season, wk.week)
            # Walk-forward safe: through_week=wk.week - 1 means this week's
            # own moneyline grading never uses a std estimated from games
            # that (in real time) hadn't been played yet -- see
            # gamma_model.gamma_residual_std()'s own docstring. Gamma's win
            # prob is ALSO the live one now (Cole's follow-up request,
            # quoting Massey's ratings theory page), so moneyline below
            # grades against gamma_home_win_prob, not Ridge's.
            gamma_win_prob_std = gamma_model.gamma_residual_std(con, season=wk.season, through_week=wk.week - 1)
        else:
            gamma_ratings = None
            gamma_win_prob_std = None

        # Same walk-forward discipline for the totals model -- rebuilt per
        # test week from only games strictly before it. See
        # src/totals_model.py for why this SP+/PPA-based regression replaced
        # the old raw-scoring-average baseline (model.totals_baseline()).
        totals_train = totals_model.load_totals_training_frame(con, before_date=wk.week_start)
        total_pipe, _ = totals_model.fit_total_model(totals_train)
        wk_totals_features = totals_model.load_upcoming_totals_frame(con, wk.season, wk.week)
        model_total_map = {}
        if not wk_totals_features.empty:
            total_preds = totals_model.predict_total(total_pipe, wk_totals_features)
            model_total_map = dict(zip(wk_totals_features["game_id"].astype(int), total_preds))
        model_total = [model_total_map.get(int(game_id)) for game_id in test_df.index]

        for i, (game_id, row) in enumerate(test_df.iterrows()):
            # ---------------------------------------------------- spread
            market_close = row["market_spread_home"]
            market_open = row["market_spread_home_open"]
            actual_margin = row["margin"]

            # THE live pick -- Dub Gamma's spread (see this module's own
            # docstring). None whenever gamma_ratings is None (any season
            # before gamma_model.SEED_SEASON) -- that game simply gets no
            # spread grade at all, same "not enough seed history yet"
            # treatment gamma_model.py/model_comparison.py already apply.
            if gamma_ratings is not None:
                is_neutral = bool(row["neutral_site"]) if pd.notna(row.get("neutral_site")) else False
                model_spread_home_i = gamma_model.predict_spread_home(
                    gamma_ratings, row["home_team"], row["away_team"], neutral_site=is_neutral)
                # THE live win prob now too (see this module's own docstring
                # and gamma_win_prob_std above) -- feeds moneyline grading
                # just below, same job Ridge's win prob used to do.
                gamma_home_win_prob_i = gamma_model.predict_home_win_prob(
                    gamma_ratings, row["home_team"], row["away_team"],
                    neutral_site=is_neutral, std=gamma_win_prob_std)
            else:
                model_spread_home_i = None
                gamma_home_win_prob_i = None

            if model_spread_home_i is not None:
                spread_grade = grade_spread_pick(
                    model_spread_home_i, market_close, market_open, actual_margin, edge_threshold)
                edge, lean = spread_grade["edge"], spread_grade["lean"]
                is_bet, bet_result, clv = spread_grade["is_bet"], spread_grade["bet_result"], spread_grade["clv_pts"]
            else:
                edge = lean = is_bet = bet_result = clv = None

            actual_home_win = 1.0 if actual_margin > 0 else (0.0 if actual_margin < 0 else 0.5)

            # ---------------------------------------------------- total (over/under)
            market_total_close = row["market_total"]
            market_total_open = row["market_total_open"]
            actual_total = row["home_points"] + row["away_points"]
            total_edge, total_lean, is_total_bet, total_bet_result, total_clv = (None,) * 5
            if pd.notna(market_total_close) and model_total[i] is not None:
                total_edge = model_total[i] - market_total_close
                # Already "the model's own lean direction" regardless of the
                # edge threshold -- unlike spread, there's no independent
                # "favorite" concept for a total to default to instead, so
                # this needs no override (see this module's docstring).
                total_lean = "Over" if total_edge > 0 else ("Under" if total_edge < 0 else "Pick'em")
                is_total_bet = abs(total_edge) >= edge_threshold and total_lean != "Pick'em"
                # Graded whenever there's a real lean at all now, same
                # "track every game" change as spread above -- only the
                # gate on is_total_bet moved, the lean computation itself
                # didn't change.
                if total_lean != "Pick'em":
                    if actual_total == market_total_close:
                        total_bet_result = "Push"
                    elif (total_lean == "Over") == (actual_total > market_total_close):
                        total_bet_result = "Win"
                    else:
                        total_bet_result = "Loss"
                    if is_total_bet and pd.notna(market_total_open):
                        # Over wants the total to have been LOW when bet and to
                        # rise by closing (market agreeing more games go over);
                        # mirror image for Under.
                        total_clv = ((market_total_close - market_total_open) if total_lean == "Over"
                                     else (market_total_open - market_total_close))

            # ---------------------------------------------------- moneyline
            # THE live win prob is Gamma's now (gamma_home_win_prob_i) --
            # None whenever gamma_ratings is None (any season before
            # gamma_model.SEED_SEASON), same "not enough seed history yet"
            # treatment spread already gets above -- moneyline simply isn't
            # graded at all for those games now (it used to always grade off
            # Ridge). See this module's own docstring.
            home_ml, away_ml = row["market_home_ml"], row["market_away_ml"]
            ml_lean, is_ml_bet, ml_bet_result, ml_profit, ml_edge = (None,) * 5
            if pd.notna(home_ml) and pd.notna(away_ml) and gamma_home_win_prob_i is not None:
                market_home_prob = no_vig_prob(home_ml, away_ml)
                ml_edge = gamma_home_win_prob_i - market_home_prob
                ml_lean = "Home" if ml_edge > 0 else "Away"
                is_ml_bet = abs(ml_edge) >= ml_edge_threshold
                if is_ml_bet:
                    home_won = actual_margin > 0  # CFB has no ties -- no push case here
                    won = home_won if ml_lean == "Home" else (not home_won)
                    ml_bet_result = "Win" if won else "Loss"
                    ml_profit = payout_profit(1.0, home_ml if ml_lean == "Home" else away_ml, won)

            results.append({
                "game_id": game_id, "season": row["season"], "week": row["week"],
                "home_team": row["home_team"], "away_team": row["away_team"],
                "is_mw_game": row["home_team"] in MW_TEAMS_2026 or row["away_team"] in MW_TEAMS_2026,
                "model_spread_home": model_spread_home_i, "market_spread_home": market_close,
                "edge": edge, "lean": lean, "is_bet": is_bet, "bet_result": bet_result, "clv": clv,
                # True only for gamma_model.SEED_SEASON's own week 1 -- see
                # this module's own docstring on why that week's grade is
                # hindsight, not a genuine out-of-sample prediction.
                "gamma_is_seed_week": bool(gamma_is_seed_week) if model_spread_home_i is not None else None,
                # THE live win prob (Gamma's) -- feeds moneyline grading
                # above and the Brier calibration score in summarize()/
                # summarize_moneyline(). None pre-gamma_model.SEED_SEASON,
                # same treatment as model_spread_home_i above.
                "home_win_prob": gamma_home_win_prob_i, "actual_home_win": actual_home_win,
                # Ridge's own win prob -- informational/candidate only now,
                # same demotion its spread already got.
                "ridge_home_win_prob": ridge_home_win_prob[i],
                "actual_margin": actual_margin,
                "model_total": model_total[i], "market_total": market_total_close,
                "total_edge": total_edge, "total_lean": total_lean, "is_total_bet": is_total_bet,
                "total_bet_result": total_bet_result, "total_clv": total_clv, "actual_total": actual_total,
                "market_home_ml": home_ml, "market_away_ml": away_ml,
                "ml_edge": ml_edge, "ml_lean": ml_lean, "is_ml_bet": is_ml_bet,
                "ml_bet_result": ml_bet_result, "ml_profit": ml_profit,
            })

    return pd.DataFrame(results)


def summarize(df: pd.DataFrame, label: str) -> dict:
    """
    Spread (ATS) summary -- flat -110 odds, the standard spread price.

    The headline record here is now the FULL record -- every graded game,
    including the ones below EDGE_THRESHOLD that default to the market
    favorite (see run_backtest()'s own docstring) -- not just the subset
    that cleared the edge threshold as a real recommended bet. Those real
    edges are still broken out separately below (n_real_edge_bets etc.),
    since "would this have made money if I only bet my actual edges" is a
    different, still-useful question from "how does the model do overall."
    """
    if df.empty:
        return {"slice": label, "n_games": 0}
    bets = df[df["bet_result"].isin(["Win", "Loss"])]
    wins = (bets["bet_result"] == "Win").sum()
    losses = (bets["bet_result"] == "Loss").sum()
    pushes = (df["bet_result"] == "Push").sum()
    n_bets = wins + losses
    ats_win_rate = wins / n_bets if n_bets else float("nan")
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n_bets if n_bets else float("nan")
    brier = float(np.mean((df["home_win_prob"] - df["actual_home_win"]) ** 2))

    # CLV only ever exists for real, threshold-clearing edges (see
    # run_backtest() -- there's no closing line to compare against on a
    # game you never actually bet), so it's computed off the real-edge
    # slice specifically, not the full-record one above.
    real_edge_bets = df[df["is_bet"] & df["bet_result"].isin(["Win", "Loss"])]
    n_real_edge_bets = int(df["is_bet"].sum())
    real_edge_wins = int((real_edge_bets["bet_result"] == "Win").sum())
    real_edge_losses = int((real_edge_bets["bet_result"] == "Loss").sum())
    n_real_edge_decided = real_edge_wins + real_edge_losses
    real_edge_win_rate = real_edge_wins / n_real_edge_decided if n_real_edge_decided else None
    real_edge_profit = real_edge_wins * (100 / 110) - real_edge_losses * 1.0
    real_edge_roi = real_edge_profit / n_real_edge_decided if n_real_edge_decided else None
    mean_clv = real_edge_bets["clv"].mean() if n_real_edge_decided else float("nan")

    return {
        "slice": label, "n_games": len(df), "n_bets": int(n_bets),
        "wins": int(wins), "losses": int(losses), "pushes": int(pushes),
        "ats_win_rate": round(ats_win_rate, 4) if n_bets else None,
        "roi_flat_stake": round(roi, 4) if n_bets else None,
        "brier_score": round(brier, 4),
        "mean_clv_pts": round(mean_clv, 3) if n_real_edge_decided else None,
        # Secondary breakdown -- how many of the games above were a real,
        # threshold-clearing edge (vs. the market-favorite default).
        "n_real_edge_bets": n_real_edge_bets,
        "real_edge_wins": real_edge_wins, "real_edge_losses": real_edge_losses,
        "real_edge_win_rate": round(real_edge_win_rate, 4) if real_edge_win_rate is not None else None,
        "real_edge_roi": round(real_edge_roi, 4) if real_edge_roi is not None else None,
    }


def summarize_totals(df: pd.DataFrame, label: str) -> dict:
    """
    Over/under summary -- flat -110 odds, same as spread. Same "full record
    is the headline now, real-edge-only broken out separately" shape as
    summarize() above -- see that function's own docstring.
    """
    if df.empty:
        return {"slice": label, "n_games": 0}
    graded = df.dropna(subset=["model_total", "actual_total"])
    bets = df[df["total_bet_result"].isin(["Win", "Loss"])]
    wins = (bets["total_bet_result"] == "Win").sum()
    losses = (bets["total_bet_result"] == "Loss").sum()
    pushes = (df["total_bet_result"] == "Push").sum()
    n_bets = wins + losses
    win_rate = wins / n_bets if n_bets else float("nan")
    profit = wins * (100 / 110) - losses * 1.0
    roi = profit / n_bets if n_bets else float("nan")
    mae = float(np.mean(np.abs(graded["model_total"] - graded["actual_total"]))) if not graded.empty else None

    real_edge_bets = df[(df["is_total_bet"] == True) & df["total_bet_result"].isin(["Win", "Loss"])]
    n_real_edge_bets = int((df["is_total_bet"] == True).sum())
    real_edge_wins = int((real_edge_bets["total_bet_result"] == "Win").sum())
    real_edge_losses = int((real_edge_bets["total_bet_result"] == "Loss").sum())
    n_real_edge_decided = real_edge_wins + real_edge_losses
    real_edge_win_rate = real_edge_wins / n_real_edge_decided if n_real_edge_decided else None
    real_edge_profit = real_edge_wins * (100 / 110) - real_edge_losses * 1.0
    real_edge_roi = real_edge_profit / n_real_edge_decided if n_real_edge_decided else None
    mean_clv = real_edge_bets["total_clv"].mean() if n_real_edge_decided else float("nan")

    return {
        "slice": label, "n_games": len(df), "n_bets": int(n_bets),
        "wins": int(wins), "losses": int(losses), "pushes": int(pushes),
        "win_rate": round(win_rate, 4) if n_bets else None,
        "roi_flat_stake": round(roi, 4) if n_bets else None,
        "mean_abs_error_pts": round(mae, 2) if mae is not None else None,
        "mean_clv_pts": round(mean_clv, 3) if n_real_edge_decided and pd.notna(mean_clv) else None,
        "n_real_edge_bets": n_real_edge_bets,
        "real_edge_wins": real_edge_wins, "real_edge_losses": real_edge_losses,
        "real_edge_win_rate": round(real_edge_win_rate, 4) if real_edge_win_rate is not None else None,
        "real_edge_roi": round(real_edge_roi, 4) if real_edge_roi is not None else None,
    }


def summarize_moneyline(df: pd.DataFrame, label: str) -> dict:
    """Moneyline summary -- REAL odds per bet, not a flat assumed price."""
    if df.empty:
        return {"slice": label, "n_games": 0}
    bets = df[(df["is_ml_bet"] == True) & (df["ml_bet_result"].isin(["Win", "Loss"]))]
    wins = (bets["ml_bet_result"] == "Win").sum()
    losses = (bets["ml_bet_result"] == "Loss").sum()
    n_bets = wins + losses
    win_rate = wins / n_bets if n_bets else float("nan")
    profit = bets["ml_profit"].sum() if n_bets else 0.0
    roi = profit / n_bets if n_bets else float("nan")
    brier = float(np.mean((df["home_win_prob"] - df["actual_home_win"]) ** 2))
    return {
        "slice": label, "n_games": len(df), "n_bets": int(n_bets),
        "wins": int(wins), "losses": int(losses),
        "win_rate": round(win_rate, 4) if n_bets else None,
        "units_won": round(float(profit), 2) if n_bets else None,
        "roi_flat_stake": round(roi, 4) if n_bets else None,
        "brier_score": round(brier, 4),
    }


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_backtest(con)
    con.close()

    if df.empty:
        print(
            "backtest: not enough graded games yet. This needs completed games with "
            f"market lines, and at least {MIN_TRAIN_GAMES} of them before the walk-forward "
            "window starts testing -- normal early in the season or with limited history pulled."
        )
        return

    CLEAN_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CLEAN_DIR / "backtest_results.csv"
    df.to_csv(out_path, index=False)
    print(f"Per-game results written to {out_path} ({len(df)} rows)\n")

    slices = [("Overall (all FBS)", df), ("Mountain West-involved", df[df["is_mw_game"]])]
    for bet_type, fn in [("SPREAD", summarize), ("TOTAL", summarize_totals), ("MONEYLINE", summarize_moneyline)]:
        print(f"=== {bet_type} ===")
        for label, sl in slices:
            s = fn(sl, label)
            print(f"--- {s['slice']} ---")
            for k, v in s.items():
                if k != "slice":
                    print(f"  {k}: {v}")
            print()


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
