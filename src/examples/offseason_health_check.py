"""
A once-a-year (or once-per-offseason) health check, meant to be run BEFORE
a new season's games start piling up. Catches the specific things that have
already caused real problems in this project if they go unnoticed:
  - The Dub Gamma Model's Moore seed (moore_seed_2026.py) not being
    refreshed for a new season -- see that file's own docstring: it is a
    ONE-TIME paste, never auto-updated, and gamma_model.py has no way to
    know a new season needs a new seed on its own.
  - A newly-arrived or renamed FBS team with no crosswalk entry, which
    gamma_model.py already warns about per-run but which is easy to miss in
    a scroll of console output -- this script surfaces it as its own report
    section instead.
  - A silent data-pull gap (a season with an oddly low game or odds-
    coverage count) -- shown as a table so a real gap doesn't hide behind
    "well the pipeline didn't crash."
  - A stale MW_TEAMS_2026 dict after realignment (this project has already
    been through one realignment cycle -- see teams.py's own comment on the
    Boise St/Colorado St/Fresno St/San Diego St/Utah St -> Pac-12 move and
    the UTEP/Northern Illinois/North Dakota State arrivals).

This does NOT re-run any model fit or grade a season's picks -- that's what
model_comparison.py is for. If data/clean/model_comparison_results.csv
already exists (i.e. model_comparison.py has been run at least once this
season), this script's last section reads that CSV and prints a same-
season-only excerpt of it as a quick "report card," instead of re-fitting
anything itself.

This is a diagnostic, not a pipeline step -- NOT wired into run_pipeline.py.
Console report only, nothing written.

Usage:
    source .venv/bin/activate
    python src/examples/offseason_health_check.py
"""
import time
from datetime import date

import duckdb
import pandas as pd

from config import DB_PATH, CLEAN_DIR
import gamma_model
from moore_seed_2026 import SEED_SEASON
from teams import MW_TEAMS_2026, FBS_CONFERENCES


def check_game_counts(con):
    print("=== Games pulled per season (eyeball this -- a real gap won't crash anything) ===")
    df = con.execute("""
        SELECT season, COUNT(*) AS n_games,
               SUM(CASE WHEN completed THEN 1 ELSE 0 END) AS n_completed
        FROM games GROUP BY season ORDER BY season
    """).fetchdf()
    print(df.to_string(index=False))
    print(
        "Note: 2020's real count is genuinely low (COVID-shortened season) -- that's expected, "
        "not a bug. The current in-progress season is naturally partial too. Look for any OTHER "
        "completed season sitting well below its neighbors.\n"
    )


def check_line_coverage(con):
    print("=== Market spread coverage per season (a low rate here starves training rows of the "
          "market_spread_home column model.load_training_frame() needs) ===")
    df = con.execute("""
        SELECT g.season,
               COUNT(*) AS n_completed_games,
               SUM(CASE WHEN m.game_id IS NOT NULL THEN 1 ELSE 0 END) AS n_with_a_line
        FROM games g
        LEFT JOIN (SELECT DISTINCT game_id FROM lines) m ON m.game_id = g.game_id
        WHERE g.completed = TRUE
        GROUP BY g.season ORDER BY g.season
    """).fetchdf()
    df["coverage_pct"] = (100 * df["n_with_a_line"] / df["n_completed_games"]).round(1)
    print(df.to_string(index=False))
    print()


def check_moore_seed_freshness(con):
    print("=== Dub Gamma Model seed freshness ===")
    max_season_in_db = con.execute("SELECT MAX(season) FROM games").fetchone()[0]
    print(f"moore_seed_2026.SEED_SEASON = {SEED_SEASON}")
    print(f"Most recent season with games in the DB = {max_season_in_db}")
    if SEED_SEASON < max_season_in_db:
        print(
            f"  ACTION NEEDED: the DB already has {max_season_in_db} games, but the Moore seed is "
            f"still {SEED_SEASON}'s. Per moore_seed_2026.py's own docstring, this is a manual, "
            f"wholesale replace -- get {max_season_in_db}'s (or whichever new season is starting) "
            f"published Moore ratings from Cole's usual source, paste them into a new "
            f"moore_seed_{max_season_in_db}.py the same way moore_seed_2026.py was built (reuse its "
            f"MOORE_NAME_ALIASES crosswalk as a starting point -- most entries carry over unchanged), "
            f"then repoint gamma_model.py's import at the new file. Never edit moore_seed_2026.py's "
            f"existing numbers in place -- it stays a frozen historical record."
        )
    else:
        print("  Looks current -- no action needed.")
    print()


def check_gamma_seed_coverage(con):
    print("=== FBS teams missing a Moore seed rating (would silently default to a flat rating) ===")
    _, warned_teams = gamma_model.replay_ratings(con)

    # Moore doesn't rate FCS opponents at all -- an FCS "buy game" defaulting
    # to DEFAULT_SEED_RATING is expected, everyday behavior (see
    # gamma_model.py's own SCOPE note and teams.py's FBS_CONFERENCES comment
    # on exactly this FCS-vs-FBS distinction), not something to action every
    # offseason. Only an FBS team with no seed is the real, actionable gap --
    # narrow warned_teams down to teams that played in an FBS conference in
    # the DB's most recent season before deciding anything needs fixing.
    fbs_teams = set(con.execute("""
        SELECT DISTINCT team FROM (
            SELECT season, home_team AS team, home_conference AS conf FROM games
            UNION ALL
            SELECT season, away_team AS team, away_conference AS conf FROM games
        ) t
        WHERE season = (SELECT MAX(season) FROM games) AND conf IN %s
    """ % (tuple(FBS_CONFERENCES),)).fetchdf()["team"])

    actionable = sorted(set(warned_teams) & fbs_teams)
    fcs_noise = sorted(set(warned_teams) - fbs_teams)
    if actionable:
        print(
            f"  ACTION NEEDED: {len(actionable)} FBS team(s) have no entry in the current Moore seed "
            f"crosswalk and are defaulting to gamma_model.DEFAULT_SEED_RATING: {actionable}\n"
            f"  This usually means a team is new to FBS, changed its CFBD 'school' spelling, or "
            f"changed conferences -- add it to MOORE_NAME_ALIASES (or SEED_RATINGS directly) in the "
            f"current moore_seed_*.py file."
        )
    else:
        print("  Every FBS team the DB has seen this replay has a real seed rating -- no action needed.")
    if fcs_noise:
        print(f"  ({len(fcs_noise)} other unrated team(s) are FCS buy-game opponents -- expected, "
              f"Moore doesn't rate FCS teams, no action needed: {fcs_noise})")
    print()


def check_mw_roster(con):
    print("=== Mountain West roster sanity (teams.py's MW_TEAMS_2026) ===")
    current_mw = con.execute("""
        SELECT DISTINCT team, conf FROM (
            SELECT season, home_team AS team, home_conference AS conf FROM games
            UNION ALL
            SELECT season, away_team AS team, away_conference AS conf FROM games
        ) t
        WHERE season = (SELECT MAX(season) FROM games) AND conf = 'Mountain West'
    """).fetchdf()
    known = set(MW_TEAMS_2026.keys())
    seen = set(current_mw["team"])
    missing_from_dict = seen - known
    if missing_from_dict:
        print(
            f"  ACTION NEEDED: the DB shows {sorted(missing_from_dict)} playing in the Mountain West "
            f"this season, but teams.py's MW_TEAMS_2026 doesn't list them -- likely a realignment "
            f"(see that file's own comment on the last one) that needs a dict update, which also "
            f"ripples into predict_week.py's is_2026_mw_team() filter and model.py's mw_involved_flag."
        )
    else:
        print("  Every Mountain West team the DB has seen this season is already in MW_TEAMS_2026.")
    print()


def show_last_season_report_card():
    print("=== Last season's model report card ===")
    path = CLEAN_DIR / "model_comparison_results.csv"
    if not path.exists():
        print(
            f"  {path} doesn't exist yet -- run src/model_comparison.py first (it writes this file), "
            f"then re-run this check to see a same-season excerpt of it here."
        )
        return
    df = pd.read_csv(path)
    last_season = int(df["season"].max())
    sl = df[df["season"] == last_season]
    print(f"  {len(sl)} graded games from season {last_season} in {path.name}.")
    for prefix, name in [("gamma", "Dub Gamma (live)"), ("ridge", "Ridge"), ("xgb", "XGBoost")]:
        result_col = f"{prefix}_result"
        if result_col not in sl.columns:
            continue
        bets = sl[sl[result_col].isin(["Win", "Loss"])]
        wins = (bets[result_col] == "Win").sum()
        n = len(bets)
        if n:
            print(f"    {name}: {wins}-{n - wins} ({wins / n * 100:.1f}%) across {n} graded picks")
    print(
        "\n  Use this as a starting point for 'should a candidate replace Gamma as the live model' -- "
        "see the improvement playbook's promotion checklist before actually flipping predict_week.py."
    )


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    print(f"Offseason health check -- run {date.today().isoformat()}\n")
    check_game_counts(con)
    check_line_coverage(con)
    check_moore_seed_freshness(con)
    check_gamma_seed_coverage(con)
    check_mw_roster(con)
    con.close()
    show_last_season_report_card()


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
