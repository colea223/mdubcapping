"""
One-off, READ-ONLY diagnostic for predict_week.py's auto_detect_week() still
returning an earlier week than expected. Shows exactly which incomplete
game(s) are pinning the detection, and Week 1's own completed/score status,
so we can tell "stale data" apart from "a real game hasn't been played yet"
without guessing. Makes no changes to the database.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python src/diagnose_next_week.py
"""
from datetime import date

import duckdb

from config import DB_PATH

def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    today = date.today().isoformat()
    print(f"Today (per this machine): {today}\n")

    print("=== What auto_detect_week() actually sees (earliest incomplete game on/after today) ===")
    rows = con.execute("""
        SELECT season, week, home_team, away_team, start_date, completed
        FROM games
        WHERE completed = FALSE AND start_date >= ?
        ORDER BY start_date
        LIMIT 15
    """, [today]).fetchall()
    if not rows:
        print("  (none found -- auto_detect_week() would return nothing at all)")
    for r in rows:
        print(f"  season={r[0]} week={r[1]}  {r[3]} @ {r[2]}  start_date={r[4]}  completed={r[5]}")

    print("\n=== All 2026 Week 1 games and their completed/score status ===")
    wk1 = con.execute("""
        SELECT home_team, away_team, start_date, completed, home_points, away_points
        FROM games
        WHERE season = 2026 AND week = 1
        ORDER BY start_date
    """).fetchdf()
    if wk1.empty:
        print("  (no 2026 week 1 games found at all)")
    else:
        for r in wk1.itertuples():
            print(f"  {r.away_team} @ {r.home_team}  start_date={r.start_date}  "
                  f"completed={r.completed}  score={r.away_points}-{r.home_points}")

    print("\n=== 2026 Week 2 games currently in the DB ===")
    wk2 = con.execute("""
        SELECT home_team, away_team, start_date, completed
        FROM games
        WHERE season = 2026 AND week = 2
        ORDER BY start_date
        LIMIT 10
    """).fetchdf()
    if wk2.empty:
        print("  (no 2026 week 2 games found yet -- the schedule pull may not have reached that far)")
    else:
        for r in wk2.itertuples():
            print(f"  {r.away_team} @ {r.home_team}  start_date={r.start_date}  completed={r.completed}")

    con.close()


if __name__ == "__main__":
    main()
