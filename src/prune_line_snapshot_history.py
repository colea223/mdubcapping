"""
One-time cleanup: run this ONCE after your next full pipeline run finishes,
if that run happened before this fix landed.

build_db.py used to backfill line_snapshots (the table behind the Line
History chart) for every season you'd ever pulled -- 2016 through the
current one -- even though a past season's lines are frozen forever and can
never show new movement. That was pure wasted disk space, not a real
feature. build_db.py is now scoped to only the current season going
forward, but it doesn't retroactively clean up rows it already wrote before
this fix -- that's what this script does, once.

Safe to run more than once (it's just a DELETE); safe to skip entirely if
you'd rather keep the historical rows for some reason -- they're inert,
just extra bytes.

Usage:
    source .venv/bin/activate
    python src/prune_line_snapshot_history.py
"""
import duckdb
import time

from config import DB_PATH, END_YEAR


def main():
    con = duckdb.connect(str(DB_PATH))
    before = con.execute("SELECT COUNT(*) FROM line_snapshots").fetchone()[0]

    con.execute("""
        DELETE FROM line_snapshots
        WHERE game_id IN (SELECT game_id FROM games WHERE season != ?)
    """, [END_YEAR])

    after = con.execute("SELECT COUNT(*) FROM line_snapshots").fetchone()[0]
    con.close()

    removed = before - after
    print(f"line_snapshots: {before} rows -> {after} rows ({removed} old-season rows removed)")
    print(f"Kept only season {END_YEAR} -- the only season a line can still move.")
    print("Run 'VACUUM;' in a duckdb shell afterward if you want to reclaim the freed disk space immediately")
    print("(otherwise DuckDB reclaims it gradually on its own).")


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
