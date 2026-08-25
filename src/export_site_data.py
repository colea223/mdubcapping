"""
Produces the JSON files the website (docs/) reads: rankings, this week's
matchups (market lines only, Covers-style), predictions (model output + any
manually-noted injury/qualitative info), and season-to-date tracking stats.

Injuries/qualitative notes have no reliable free automated feed (CFBD doesn't
publish one), so that piece is manually maintained: edit site_notes.json in
the project root -- {"Away Team @ Home Team": "note text"} -- and this script
merges it in by matchup. Leave it out or empty and notes just render blank.

The Tracking page shows TWO separate things side by side: the model's own
hypothetical flat-1-unit-stake performance (same walk-forward grading as
backtest.py, filtered to the current season -- spread, total, AND moneyline),
and YOUR actual bets, read directly from the Bet Log tab of
excel/MW_Handicapping_Tracker.xlsx. The two are never blended into one number
-- the model's picks are a what-if; your Bet Log is what you actually staked.

Usage:
    source .venv/bin/activate
    python src/export_site_data.py
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import openpyxl
import pandas as pd

from config import DB_PATH
import model
import backtest
from odds import payout_profit
from power_rating import current_ratings
from teams import MW_TEAMS_2026
from predict_week import auto_detect_week

ROOT = Path(__file__).resolve().parent.parent
DOCS_DATA = ROOT / "docs" / "data"
NOTES_PATH = ROOT / "site_notes.json"
TRACKER_PATH = ROOT / "excel" / "MW_Handicapping_Tracker.xlsx"
BET_LOG_ROWS = range(2, 43)   # matches excel/build_tracker.py's layout
CURRENT_SEASON = 2026


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_notes():
    if NOTES_PATH.exists():
        return json.loads(NOTES_PATH.read_text())
    return {}


def team_record(con, team, season):
    row = con.execute("""
        SELECT
            SUM(CASE WHEN home_team = ? AND home_points > away_points THEN 1
                     WHEN away_team = ? AND away_points > home_points THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN (home_team = ? AND home_points < away_points)
                       OR (away_team = ? AND away_points < home_points) THEN 1 ELSE 0 END) AS losses
        FROM games
        WHERE completed = TRUE AND (home_team = ? OR away_team = ?) AND season = ?
    """, [team, team, team, team, team, team, season]).fetchone()
    wins, losses = row[0] or 0, row[1] or 0
    return wins, losses


def team_rating_trend(con, team):
    rows = con.execute("""
        SELECT r.rating_after, g.start_date
        FROM ratings_baseline r
        JOIN games g ON g.game_id = r.game_id
        WHERE r.team = ?
        ORDER BY g.start_date DESC
        LIMIT 2
    """, [team]).fetchall()
    if len(rows) < 2:
        return 0.0
    return round(rows[0][0] - rows[1][0], 1)


def build_rankings(con):
    ratings = current_ratings(con)
    rows = []
    for team in MW_TEAMS_2026:
        rating = ratings.get(team)
        wins, losses = team_record(con, team, CURRENT_SEASON)
        trend = team_rating_trend(con, team)
        rows.append({
            "team": team,
            "rating": round(rating, 1) if rating is not None else None,
            "wins": wins, "losses": losses,
            "trend": trend,
        })
    rows.sort(key=lambda r: (r["rating"] is None, -(r["rating"] or 0)))
    for i, r in enumerate(rows, start=1):
        r["rank"] = i
    return rows


def mw_game(row_home, row_away):
    return row_home in MW_TEAMS_2026 or row_away in MW_TEAMS_2026


def build_matchups_and_predictions(con, notes: dict):
    detected = auto_detect_week(con)
    if detected is None:
        return [], [], None
    season, week = detected[0], detected[1]

    games = con.execute("""
        SELECT game_id, season, week, start_date, home_team, away_team
        FROM games WHERE season = ? AND week = ?
        ORDER BY start_date
    """, [season, week]).fetchdf()
    games = games[games.apply(lambda r: mw_game(r["home_team"], r["away_team"]), axis=1)]

    lines = con.execute("""
        SELECT game_id, AVG(spread) AS spread, AVG(over_under) AS total,
               AVG(home_moneyline) AS home_ml, AVG(away_moneyline) AS away_ml
        FROM lines GROUP BY game_id
    """).fetchdf().set_index("game_id")

    matchups = []
    for g in games.itertuples():
        line = lines.loc[g.game_id] if g.game_id in lines.index else None
        matchups.append({
            "game_id": int(g.game_id), "week": int(g.week),
            "date": pd.to_datetime(g.start_date).strftime("%Y-%m-%d"),
            "away_team": g.away_team, "home_team": g.home_team,
            "market_spread_home": round(line["spread"], 1) if line is not None and pd.notna(line["spread"]) else None,
            "market_total": round(line["total"], 1) if line is not None and pd.notna(line["total"]) else None,
            "home_moneyline": round(line["home_ml"]) if line is not None and pd.notna(line["home_ml"]) else None,
            "away_moneyline": round(line["away_ml"]) if line is not None and pd.notna(line["away_ml"]) else None,
        })

    predictions = []
    train_df = model.load_training_frame(con)
    if len(train_df) >= 10 and not games.empty:
        pipe, residual_std = model.fit_margin_model(train_df)
        upcoming = model.load_upcoming_frame(con, season, week)
        upcoming = upcoming[upcoming["game_id"].isin(games["game_id"])]
        if not upcoming.empty:
            pred_margin = model.predict_margin(pipe, upcoming)
            model_spread_home = -pred_margin
            home_win_prob = model.margin_to_home_win_prob(pred_margin, residual_std)
            team_latest, league_avg = model.totals_baseline(con)

            m_by_id = {m["game_id"]: m for m in matchups}
            for i, row in enumerate(upcoming.itertuples()):
                mkt = m_by_id.get(row.game_id, {})
                edge = (mkt.get("market_spread_home") - model_spread_home[i]) if mkt.get("market_spread_home") is not None else None
                note_key = f"{row.away_team} @ {row.home_team}"
                predictions.append({
                    "game_id": int(row.game_id), "week": int(row.week),
                    "away_team": row.away_team, "home_team": row.home_team,
                    "model_spread_home": round(model_spread_home[i], 1),
                    "model_total": round(model.predict_total_for_matchup(team_latest, league_avg, row.home_team, row.away_team), 1),
                    "home_win_prob": round(float(home_win_prob[i]), 3),
                    "edge": round(edge, 1) if edge is not None else None,
                    "notes": notes.get(note_key, ""),
                })

    return matchups, predictions, {"season": season, "week": week}


def _bet_type_block(summary: dict, win_rate_key: str) -> dict:
    n_bets = summary.get("n_bets", 0) or 0
    return {
        "n_bets": n_bets,
        "wins": summary.get("wins", 0),
        "losses": summary.get("losses", 0),
        "pushes": summary.get("pushes", 0),
        "win_rate": summary.get(win_rate_key),
        "units": summary.get("units_won") if "units_won" in summary else round((summary.get("roi_flat_stake") or 0) * n_bets, 2),
    }


def build_model_tracking(con):
    df = backtest.run_backtest(con)
    if df.empty:
        return {"n_games": 0, "note": "Not enough graded history yet."}

    season_df = df[df["season"] == CURRENT_SEASON]
    mw_df = season_df[season_df["is_mw_game"]]

    spread = backtest.summarize(mw_df, "spread")
    total = backtest.summarize_totals(mw_df, "total")
    moneyline = backtest.summarize_moneyline(mw_df, "moneyline")

    bets = mw_df[mw_df["is_bet"] & mw_df["bet_result"].isin(["Win", "Loss"])]
    upsets = bets[bets.apply(
        lambda r: (r["lean"] == "Home" and r["market_spread_home"] > 0)
        or (r["lean"] == "Away" and r["market_spread_home"] < 0), axis=1
    )] if not bets.empty else bets
    upset_wins = int((upsets["bet_result"] == "Win").sum()) if not upsets.empty else 0
    upset_total = len(upsets)

    return {
        "season": CURRENT_SEASON,
        "n_games": spread.get("n_games", 0),
        "spread": _bet_type_block(spread, "ats_win_rate"),
        "total": _bet_type_block(total, "win_rate"),
        "moneyline": _bet_type_block(moneyline, "win_rate"),
        "upset_calls": upset_total,
        "upset_wins": upset_wins,
        "note": "Hypothetical flat 1-unit-per-bet tracking of the model's own picks (spread/total at -110, "
                "moneyline at the real posted odds) -- not your personal bets.",
    }


def read_bet_log(tracker_path: Path = TRACKER_PATH):
    """
    Your actual placed bets, straight from the Bet Log tab -- read fresh from
    the raw input cells (Odds, Stake, Result) and re-computed with the same
    odds math as the model's own grading, rather than trusting the sheet's
    cached formula values (openpyxl never recalculates formulas itself, so a
    cached value is only as fresh as the last time the file was opened in
    real Excel).
    """
    if not tracker_path.exists():
        return {"n_bets": 0, "note": "Tracker workbook not found -- run excel/build_tracker.py first."}

    wb = openpyxl.load_workbook(tracker_path, data_only=False)
    if "Bet Log" not in wb.sheetnames:
        return {"n_bets": 0, "note": "No Bet Log tab found in the tracker workbook."}
    ws = wb["Bet Log"]

    by_type = {}   # bet type -> {wins, losses, pushes, units}
    for r in BET_LOG_ROWS:
        bet_type = ws[f"D{r}"].value
        odds = ws[f"G{r}"].value
        stake = ws[f"H{r}"].value
        result = ws[f"K{r}"].value
        if not bet_type or odds is None or stake is None or not result:
            continue
        result = str(result).strip().upper()
        if result not in ("W", "L", "P"):
            continue

        bucket = by_type.setdefault(str(bet_type).strip(), {"wins": 0, "losses": 0, "pushes": 0, "units": 0.0})
        if result == "P":
            bucket["pushes"] += 1
        else:
            won = result == "W"
            bucket["wins" if won else "losses"] += 1
            bucket["units"] += payout_profit(float(stake), float(odds), won)

    if not by_type:
        return {"n_bets": 0, "note": "No graded bets in the Bet Log yet -- add a Result (W/L/P) to see your record here."}

    by_type_out = {}
    total_wins = total_losses = total_pushes = 0
    total_units = 0.0
    for bet_type, b in by_type.items():
        n_bets = b["wins"] + b["losses"]
        by_type_out[bet_type] = {
            "wins": b["wins"], "losses": b["losses"], "pushes": b["pushes"],
            "win_rate": round(b["wins"] / n_bets, 4) if n_bets else None,
            "units": round(b["units"], 2),
        }
        total_wins += b["wins"]
        total_losses += b["losses"]
        total_pushes += b["pushes"]
        total_units += b["units"]

    total_n_bets = total_wins + total_losses
    return {
        "n_bets": total_n_bets,
        "wins": total_wins, "losses": total_losses, "pushes": total_pushes,
        "win_rate": round(total_wins / total_n_bets, 4) if total_n_bets else None,
        "units": round(total_units, 2),
        "by_type": by_type_out,
        "note": "Your actual bets from the Bet Log tab, graded at the real odds you entered.",
    }


def main():
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    notes = load_notes()

    rankings = build_rankings(con)
    matchups, predictions, week_info = build_matchups_and_predictions(con, notes)
    tracking = {"model": build_model_tracking(con), "yours": read_bet_log()}
    con.close()

    meta = {"generated_at": _now_iso(), "current_week": week_info}

    (DOCS_DATA / "rankings.json").write_text(json.dumps({"meta": meta, "rankings": rankings}, indent=2))
    (DOCS_DATA / "matchups.json").write_text(json.dumps({"meta": meta, "matchups": matchups}, indent=2))
    (DOCS_DATA / "predictions.json").write_text(json.dumps({"meta": meta, "predictions": predictions}, indent=2))
    (DOCS_DATA / "tracking.json").write_text(json.dumps({"meta": meta, "tracking": tracking}, indent=2))

    print(f"Wrote rankings ({len(rankings)}), matchups ({len(matchups)}), "
          f"predictions ({len(predictions)}), tracking summary to {DOCS_DATA}")


if __name__ == "__main__":
    main()
