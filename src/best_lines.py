"""
Line shopping: for every upcoming game, shows Dub Gamma's number next to what EVERY sportsbook in the database is
offering, and which book has the best price for the side Gamma likes.

Writes docs/data/best_lines.json (for the site) and data/clean/best_lines.csv, and prints a table.

Conventions (same as the rest of the project): spreads are from the HOME team's perspective, negative = home favored.
  * A HOME pick wants the HIGHEST home spread (home +3 beats home +2.5).
  * An AWAY pick wants the LOWEST home spread (home -2.5 means away +2.5, better than home -3).
  * edge = book spread - model spread; positive = value on Home, negative = value on Away. The pick uses the same
    rule as everywhere else (|edge| < 1 against the consensus line = follow the market favorite).

Source: the `lines` table (one row per game per sportsbook, refreshed by pull_lines.py / pull_odds_api.py).
Honest limits: only the books your CFBD / Odds API plans return are shown, and a line is only as fresh as the last pull
(pulled_at is shown). Lines move; re-run the workflow right before you bet.
"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import ROOT, CLEAN_DIR  # noqa: E402

OUT_JSON = ROOT / "docs" / "data" / "best_lines.json"
OUT_CSV = CLEAN_DIR / "best_lines.csv"
FAVORITE_DEFAULT_EDGE = 1.0
VALUE_EDGE = 2.0


def pick_side(model_spread, consensus):
    edge = consensus - model_spread
    if abs(edge) < FAVORITE_DEFAULT_EDGE and consensus != 0:
        return "Home" if consensus < 0 else "Away"
    return "Home" if edge > 0 else ("Away" if edge < 0 else None)


def rank_books(books, side):
    """books: {name: home_spread}. Best price first for the given side."""
    items = [(n, s) for n, s in books.items() if s is not None]
    return sorted(items, key=lambda x: -x[1] if side == "Home" else x[1])


def build_game_row(game, model_spread, books):
    vals = [s for s in books.values() if s is not None]
    if not vals:
        return None
    consensus = sum(vals) / len(vals)
    side = pick_side(model_spread, consensus)
    row = {"game_id": game["game_id"], "home": game["home"], "away": game["away"], "kickoff": game.get("kickoff"),
           "model_spread_home": round(model_spread, 1), "consensus_spread_home": round(consensus, 1),
           "n_books": len(vals), "lean": side, "books": {n: s for n, s in sorted(books.items())}}
    if side is None:
        return row
    ranked = rank_books(books, side)
    best_book, best = ranked[0]
    worst_book, worst = ranked[-1]
    row.update({
        "edge_vs_consensus": round(consensus - model_spread, 1),
        "best_book": best_book, "best_spread_home": best,
        "edge_at_best": round(best - model_spread, 1),
        "worst_book": worst_book, "worst_spread_home": worst,
        "points_gained_by_shopping": round(abs(best - worst), 1),
        "best_is_value": bool(abs(best - model_spread) >= VALUE_EDGE and (best - model_spread > 0) == (side == "Home")),
    })
    return row


def _fmt(s):
    return f"{s:+.1f}" if s is not None else "  - "


def main():
    try:
        _main()
    except Exception as e:  # noqa: BLE001 - never break the pipeline
        print(f"  [best lines] step failed and was skipped: {type(e).__name__}: {e}")


def _main():
    import duckdb
    from config import DB_PATH
    import gamma_model
    from teams import normalize_team_name

    con = duckdb.connect(str(DB_PATH))
    season = con.execute("SELECT MAX(season) FROM games").fetchone()[0]
    week = gamma_model._next_upcoming_week(con, season)
    if week is None:
        print("  [best lines] no upcoming week found -- skipped.")
        return
    ratings, _ = gamma_model.replay_ratings(con, season=season)
    games = con.execute("""
        SELECT game_id, home_team, away_team, start_date, neutral_site FROM games
        WHERE season = ? AND week = ? AND season_type = 'regular' AND (completed IS NULL OR completed = FALSE)
        ORDER BY start_date
    """, [season, week]).fetchall()
    rows = []
    for gid, home, away, start, neutral in games:
        books = {p: s for p, s in con.execute(
            "SELECT provider, spread FROM lines WHERE game_id = ? AND spread IS NOT NULL", [gid]).fetchall()}
        h, a = normalize_team_name(home), normalize_team_name(away)
        if not books or h not in ratings or a not in ratings:
            continue
        spread = gamma_model.predict_spread_home(ratings, h, a, neutral_site=bool(neutral))
        r = build_game_row({"game_id": gid, "home": home, "away": away,
                            "kickoff": start.isoformat() if start is not None else None}, float(spread), books)
        if r:
            rows.append(r)
    try:
        stamp = con.execute("SELECT MAX(pulled_at) FROM line_snapshots").fetchone()[0]
    except Exception:  # noqa: BLE001
        stamp = None
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps({"season": season, "week": week, "lines_last_pulled": str(stamp) if stamp else None,
                                    "games": rows}, indent=1), encoding="utf-8")
    flat = [{k: v for k, v in r.items() if k != "books"} for r in rows]
    cols = sorted({k for r in flat for k in r})
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(flat)
    print(f"  [best lines] week {week}: {len(rows)} games, lines last pulled {stamp}")
    print(f"    {'game':<38}{'model':>7}{'cons':>7}  pick   best book (number)          shop gain")
    for r in sorted(rows, key=lambda x: -abs(x.get("edge_at_best") or 0)):
        if r["lean"] is None:
            continue
        flag = " *VALUE*" if r["best_is_value"] else ""
        print(f"    {r['away'][:17] + ' @ ' + r['home'][:17]:<38}{_fmt(r['model_spread_home']):>7}"
              f"{_fmt(r['consensus_spread_home']):>7}  {r['lean']:<5}  {r['best_book'][:16]:<16} {_fmt(r['best_spread_home']):>6}"
              f"   {r['points_gained_by_shopping']:>4.1f}{flag}")


if __name__ == "__main__":
    main()
