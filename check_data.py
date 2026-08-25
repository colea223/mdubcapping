"""
Quick diagnostic: how much data is actually in the database, and how much of
it has a market line attached (which is what backtest.py needs).

Usage (from the project root, venv active):
    python check_data.py
"""
import duckdb

con = duckdb.connect("db/mw_handicapping.duckdb")

print("completed games:", con.execute("SELECT COUNT(*) FROM games WHERE completed = TRUE").fetchone()[0])
print("rows in lines table:", con.execute("SELECT COUNT(*) FROM lines").fetchone()[0])
print("rows in venues table:", con.execute("SELECT COUNT(*) FROM venues").fetchone()[0])
print("rows in ratings_baseline table:", con.execute("SELECT COUNT(*) FROM ratings_baseline").fetchone()[0])
print("rows in game_features table:", con.execute("SELECT COUNT(*) FROM game_features").fetchone()[0])

print("\nlines rows by season:")
print(con.execute("SELECT season, COUNT(*) AS n FROM lines GROUP BY season ORDER BY season").fetchdf())

n = con.execute("""
    SELECT COUNT(*) FROM games g
    WHERE g.completed = TRUE
      AND g.game_id IN (SELECT game_id FROM lines)
""").fetchone()[0]
print("\ncompleted games WITH a market line:", n)

con.close()