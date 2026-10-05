"""
Diagnostic for the "weekly ATS looks wrong on conference tabs" report.
Run from the project root with the venv active:
    python diagnose_ats_weekly.py
"""
import sys
sys.path.insert(0, "src")
sys.path.insert(0, "excel")

import duckdb
from config import DB_PATH
import backtest
from teams import FBS_CONFERENCES

CURRENT_SEASON = 2026
CONF = "American Athletic"

con = duckdb.connect(str(DB_PATH))

# 1) Any duplicate game_id rows in the games table itself?
dupe_games = con.execute("""
    SELECT game_id, COUNT(*) AS n FROM games GROUP BY game_id HAVING COUNT(*) > 1
""").fetchdf()
print(f"[1] Duplicate game_id rows in games table: {len(dupe_games)}")
if len(dupe_games):
    print(dupe_games.head(10))

# 2) Run the exact same backtest used by update_tracker.py
bt = backtest.run_backtest(con)
bt = bt[bt["season"] == CURRENT_SEASON]
print(f"[2] bt rows for season {CURRENT_SEASON}: {len(bt)}  (unique game_id: {bt['game_id'].nunique()})")

dupe_bt = bt[bt.duplicated(subset=["game_id"], keep=False)]
print(f"    duplicate game_id rows WITHIN bt: {len(dupe_bt)}")
if len(dupe_bt):
    print(dupe_bt[["game_id", "week", "home_team", "away_team", "bet_result"]].sort_values("game_id").head(20))

# 3) Conference lookup exactly as update_tracker.py does it
conf_by_game = {
    gid: (hc, ac) for gid, hc, ac in con.execute(
        "SELECT game_id, home_conference, away_conference FROM games WHERE season = ?", [CURRENT_SEASON]
    ).fetchall()
}
print(f"[3] conf_by_game entries: {len(conf_by_game)}")

graded_rows = []
for row in bt.itertuples():
    if row.bet_result is None:
        continue
    gid = int(row.game_id)
    hc, ac = conf_by_game.get(gid, (None, None))
    graded_rows.append({
        "game_id": gid, "bet_result": row.bet_result, "week": int(row.week),
        "home_conf": hc, "away_conf": ac,
        "home_team": row.home_team, "away_team": row.away_team,
    })

aac_rows = [r for r in graded_rows if r["home_conf"] == CONF or r["away_conf"] == CONF]
print(f"[4] {CONF} graded rows (all weeks): {len(aac_rows)}")
by_week = {}
for r in aac_rows:
    by_week.setdefault(r["week"], []).append(r)
for wk in sorted(by_week):
    rows = by_week[wk]
    w = sum(1 for r in rows if r["bet_result"] == "Win")
    l = sum(1 for r in rows if r["bet_result"] == "Loss")
    p = sum(1 for r in rows if r["bet_result"] == "Push")
    ids = [r["game_id"] for r in rows]
    dup_ids = len(ids) != len(set(ids))
    print(f"    week {wk}: {len(rows)} row(s) -> {w}-{l}-{p}  duplicate game_ids: {dup_ids}")
    if wk == max(by_week):
        print("    --- detail for latest week ---")
        for r in rows:
            print(f"      game_id={r['game_id']} {r['away_team']} @ {r['home_team']} "
                  f"home_conf={r['home_conf']} away_conf={r['away_conf']} result={r['bet_result']}")

con.close()
