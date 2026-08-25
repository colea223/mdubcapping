"""
Produces the JSON files the website (docs/) reads: rankings, this week's
matchups (market lines only, Covers-style), predictions (model output + any
manually-noted injury/qualitative info), and season-to-date tracking stats.

Injuries/qualitative notes have no reliable free automated feed (CFBD doesn't
publish one), so that piece is manually maintained: edit site_notes.json in
the project root -- {"Away Team @ Home Team": "note text"} -- and this script
merges it in by matchup. Leave it out or empty and notes just render blank.

Tracking numbers reflect the MODEL's own hypothetical flat-1-unit-stake
performance (same walk-forward grading as backtest.py, filtered to the current
season), not necessarily your actual personal bets/stakes -- those live in
your private Excel Bet Log, which this script doesn't read.

Usage:
    source .venv/bin/activate
    python src/export_site_data.py
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from config import DB_PATH
import model
import backtest
from power_rating import current_ratings
from teams import MW_TEAMS_2026
from predict_week import auto_detect_week

ROOT = Path(__file__).resolve().parent.parent
DOCS_DATA = ROOT / "docs" / "data"
NOTES_PATH = ROOT / "site_notes.json"
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


def build_tracking(con):
    df = backtest.run_backtest(con)
    if df.empty:
        return {"n_games": 0, "n_bets": 0, "note": "Not enough graded history yet."}

    season_df = df[df["season"] == CURRENT_SEASON]
    mw_df = season_df[season_df["is_mw_game"]]
    summary = backtest.summarize(mw_df, "season_mw")

    bets = mw_df[mw_df["is_bet"] & mw_df["bet_result"].isin(["Win", "Loss"])]
    upsets = bets[bets.apply(
        lambda r: (r["lean"] == "Home" and r["market_spread_home"] > 0)
        or (r["lean"] == "Away" and r["market_spread_home"] < 0), axis=1
    )] if not bets.empty else bets
    upset_wins = int((upsets["bet_result"] == "Win").sum()) if not upsets.empty else 0
    upset_total = len(upsets)

    return {
        "season": CURRENT_SEASON,
        "n_games": summary.get("n_games", 0),
        "n_bets": summary.get("n_bets", 0),
        "wins": summary.get("wins", 0),
        "losses": summary.get("losses", 0),
        "pushes": summary.get("pushes", 0),
        "ats_win_rate": summary.get("ats_win_rate"),
        "units": round((summary.get("roi_flat_stake") or 0) * (summary.get("n_bets") or 0), 2),
        "upset_calls": upset_total,
        "upset_wins": upset_wins,
        "note": "Hypothetical flat 1-unit-per-bet tracking of the model's own picks -- not your personal bet log.",
    }


def main():
    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    notes = load_notes()

    rankings = build_rankings(con)
    matchups, predictions, week_info = build_matchups_and_predictions(con, notes)
    tracking = build_tracking(con)
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
