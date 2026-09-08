"""
Walk-forward A/B harness for a candidate injury_diff feature. NOT yet
computed in game_features (unlike sos_diff/returning_production_diff/
qb_continuity_diff, which diagnose_new_features.py already tests) -- this
harness builds it itself from injury_reports + player_season_ppa and
injects it into model.load_training_frame()'s output via a monkeypatch,
so the idea can be tested WITHOUT a schema/features.py change before it's
even proven to help. Same "prove it in a real backtest first" discipline
this project already applied to sos_diff/returning_production_diff/
qb_continuity_diff (diagnose_new_features.py) and the down/distance
situational splits (diagnose_situational_features.py).

WHY THIS ISN'T JUST GAMMA'S EXISTING INJURY TERM, REUSED:
gamma_model.py's own load_injury_weights_by_week() already builds a very
similar signal for Dub Gamma, but weights each missing player by their
CURRENT-season total_ppa (player_season_ppa is a single season-cumulative
"latest wins" total, not week-by-week -- see that table's own schema.sql
comment). That's fine for Gamma, which was never held to Ridge/XGBoost's
walk-forward no-leakage discipline (see model.py's own extensive comments
on why sp_diff/ppa_diff/talent_diff/the 7 drive-based diffs are all
whole-PRIOR-season only, never in-progress). Using a player's full
(eventual) CURRENT-season PPA to weight an early-season injury would leak
information the model couldn't actually have known yet at prediction time.
This harness instead weights each injured player by their PRIOR season's
total_ppa -- same safe-by-construction convention as every other _diff
feature already in FEATURE_COLS -- so a true freshman/transfer with no
prior-season row simply contributes 0 (conservative default, not a crash).
Cole: if this feature earns its way into FEATURE_COLS, gamma_model.py's OWN
version likely deserves the same prior-season fix at some point -- separate
change, flagging it here so it isn't lost.

Sign convention matches every other *_diff column (home minus away, see
features.py): injury_diff = away_burden - home_burden, so a POSITIVE value
means the AWAY team is hurt worse -- same direction as rating_diff etc.,
where positive already means "advantage home."

Section 1: coverage. injury_reports is a live scrape of covers.com's
CURRENT injury report (see pull_injuries.py's own docstring), not a
historical archive -- don't expect this to cover 2016-2025 the way
sp_diff/ppa_diff do. A feature this thin can still help in the specific
weeks it fires (the imputer fills everything else with the training pool's
median, same treatment every other partial-coverage feature in
FEATURE_COLS already gets) -- but the OVERALL backtest number below will be
diluted by however many zero-coverage weeks it's averaged across. Read the
coverage count before over- or under-trusting the overall result.

Section 2: same walk-forward A/B as diagnose_new_features.py -- baseline
FEATURE_COLS vs. baseline + injury_diff, overall and Mountain-West-involved
slice, PLUS (since coverage is likely thin) a third comparison restricted
to just the games that actually had real injury_diff signal -- that subset
is where any real effect should show up most clearly, since it isn't
diluted by median-imputed zero-signal weeks.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python src/diagnose_injury_feature.py
"""
import time

import duckdb
import pandas as pd

from config import DB_PATH
import model
from backtest import run_backtest, summarize
from pull_injuries import status_weight


def build_injury_diff_by_game(con):
    """
    Returns {game_id: injury_diff}. A game_id is included ONLY if at least
    one side has real injury-report + prior-season-PPA coverage -- a game
    with no signal at all is left OUT of the dict entirely (not defaulted to
    0.0), so the downstream .map() produces NaN for it and the imputer fills
    it with the training pool's median, same as every other partial-
    coverage feature. A hard 0.0 would wrongly claim "both teams fully
    healthy" for a week we simply have no report for.

    Empty dict if injury_reports/player_season_ppa don't exist yet or have
    no rows -- same best-effort, never-crash philosophy as gamma_model.py's
    own load_injury_weights_by_week().
    """
    try:
        games = con.execute("SELECT game_id, season, week, home_team, away_team FROM games").fetchall()
        injuries = con.execute(
            "SELECT season, week, team, player_last_name, status FROM injury_reports"
        ).fetchall()
    except duckdb.CatalogException:
        return {}
    if not injuries:
        return {}

    try:
        ppa_rows = con.execute(
            "SELECT season, player_name, team, total_ppa FROM player_season_ppa"
        ).fetchall()
    except duckdb.CatalogException:
        ppa_rows = []

    # PRIOR-season total_ppa by (team, last_name, season) -- see this file's
    # own docstring on why prior season, not current, unlike gamma_model.py's
    # version. On a last-name collision within the same team+season, the
    # largest-total_ppa candidate wins -- same tie-break gamma_model.py uses,
    # for the same reason (the more-featured player is overwhelmingly the
    # likelier match for a reported injury).
    ppa_by_team_lastname_season = {}
    for season, name, team, total_ppa in ppa_rows:
        if not name or total_ppa is None:
            continue
        last = name.strip().split(" ")[-1].lower()
        key = (team, last, season)
        if key not in ppa_by_team_lastname_season or total_ppa > ppa_by_team_lastname_season[key]:
            ppa_by_team_lastname_season[key] = total_ppa

    # (team, season, week) -> summed injury burden, using the PRIOR season's PPA
    burden = {}
    for season, week, team, last_name, status in injuries:
        if not last_name:
            continue
        prior_value = ppa_by_team_lastname_season.get((team, last_name.strip().lower(), season - 1))
        if prior_value is None or prior_value <= 0:
            continue  # unmatched, or a replacement-level/negative prior season -- contributes nothing
        mult, _ = status_weight(status or "")
        key = (team, season, week)
        burden[key] = burden.get(key, 0.0) + mult * prior_value

    result = {}
    for game_id, season, week, home_team, away_team in games:
        home_key = (home_team, season, week)
        away_key = (away_team, season, week)
        if home_key not in burden and away_key not in burden:
            continue  # no injury signal for either side this week -- leave OUT, not a fabricated 0
        result[game_id] = burden.get(away_key, 0.0) - burden.get(home_key, 0.0)
    return result


def section1_coverage(injury_diff_by_game, train_df):
    print("=" * 78)
    print("1. Coverage: how many training rows have real injury_diff signal, which weeks")
    print("=" * 78)
    if not injury_diff_by_game:
        print(
            "  0 games -- injury_reports and/or player_season_ppa are empty or don't exist "
            "yet on this machine.\n  Run src/pull_injuries.py and src/pull_stats.py (player "
            "season PPA), then src/build_db.py, before this harness has anything to test.\n"
        )
        return False
    covered_ids = set(injury_diff_by_game)
    covered = train_df[train_df["game_id"].isin(covered_ids)]
    print(f"  {len(covered)} / {len(train_df)} training rows have real injury_diff signal")
    if not covered.empty:
        for s in sorted(covered["season"].unique()):
            weeks = sorted(covered[covered["season"] == s]["week"].unique())
            print(f"    season {int(s)}: weeks {[int(w) for w in weeks]}")
    print(
        "\n  NOTE: pull_injuries.py scrapes covers.com's CURRENT injury report, not a "
        "historical archive -- expect coverage limited to whatever weeks it's actually been "
        "run on, nowhere near the full 2016+ history sp_diff/ppa_diff/talent_diff have. A "
        "thin-coverage feature can still help in the weeks it fires (the imputer fills "
        "everything else with the training pool's median) -- but don't expect a big move in "
        "the OVERALL number in section 2 if coverage is only a few weeks; the "
        "injury-covered-games-only comparison at the end of section 2 is where a real effect "
        "should actually show up.\n"
    )
    return True


def section2_backtest(con, injury_diff_by_game):
    print("=" * 78)
    print("2. Walk-forward backtest: baseline vs. baseline + injury_diff")
    print("   (2 backtests in this one process -- current model.py state is restored after)")
    print("=" * 78)
    saved_cols = model.FEATURE_COLS
    saved_loader = model.load_training_frame

    def patched_loader(con, before_date=None):
        df = saved_loader(con, before_date=before_date)
        df["injury_diff"] = df["game_id"].map(injury_diff_by_game)
        return df

    variants = [
        ("baseline (no injury_diff)", saved_cols),
        ("+ injury_diff", saved_cols + ["injury_diff"]),
    ]

    results = {}
    try:
        model.load_training_frame = patched_loader
        for label, cols in variants:
            model.FEATURE_COLS = cols
            t0 = time.time()
            df = run_backtest(con)
            print(f"  ran '{label}': {time.time() - t0:.1f}s ({len(df)} graded games)")
            results[label] = df
    finally:
        model.FEATURE_COLS = saved_cols
        model.load_training_frame = saved_loader  # don't leave the module patched for anything after this
    print()

    baseline_label, _ = variants[0]
    new_label, _ = variants[1]
    baseline_df, new_df = results[baseline_label], results[new_label]

    if baseline_df.empty:
        print(
            "  No graded games in the baseline run -- not enough completed games with a "
            "market line yet (need MIN_TRAIN_GAMES worth of history). Nothing to compare.\n"
        )
        return

    print(f"  {baseline_label}")
    print(f"    overall: {summarize(baseline_df, 'overall')}")
    print(f"    MW:      {summarize(baseline_df[baseline_df['is_mw_game']], 'MW')}")
    print()

    if new_df.empty:
        print(f"  {new_label}\n    (no graded games)\n")
        return

    print(f"  {new_label}")
    print(f"    overall: {summarize(new_df, 'overall')}")
    print(f"    MW:      {summarize(new_df[new_df['is_mw_game']], 'MW')}")

    merged = baseline_df[["game_id", "lean"]].merge(
        new_df[["game_id", "lean"]], on="game_id", suffixes=("_base", "_new"),
    )
    flipped = (merged["lean_base"] != merged["lean_new"]).sum()
    print(f"    {flipped} of {len(merged)} games flipped which side the model leaned toward vs. baseline")

    # The comparison that matters most given thin coverage: just the graded
    # games that actually had real (non-imputed) injury_diff signal -- not
    # diluted by every other test week's median-filled value.
    covered_game_ids = set(injury_diff_by_game) & set(new_df["game_id"])
    if covered_game_ids:
        base_cov = baseline_df[baseline_df["game_id"].isin(covered_game_ids)]
        new_cov = new_df[new_df["game_id"].isin(covered_game_ids)]
        print(f"\n  On just the {len(covered_game_ids)} graded games with REAL injury_diff signal:")
        print(f"    baseline: {summarize(base_cov, 'baseline, injury-covered games')}")
        print(f"    +injury:  {summarize(new_cov, '+injury, injury-covered games')}")
    else:
        print("\n  None of the injury-covered games made it into a graded backtest week.")
    print()


def main():
    con = duckdb.connect(str(DB_PATH))
    injury_diff_by_game = build_injury_diff_by_game(con)

    train_df = model.load_training_frame(con)
    if train_df.empty:
        print("diagnose_injury_feature: no training data yet -- run the full pipeline first.")
        con.close()
        return

    has_coverage = section1_coverage(injury_diff_by_game, train_df)
    if not has_coverage:
        con.close()
        return

    section2_backtest(con, injury_diff_by_game)
    con.close()


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
