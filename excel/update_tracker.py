"""
Phase 4: wires the model's output into MW_Handicapping_Tracker.xlsx so you're
not copy-pasting numbers by hand every week.

What it writes (and ONLY this -- everything else in the workbook, including
your Bet Log and any notes you've typed, is left untouched):
  - Weekly Slate: Week, Date, Away Team, Home Team, Model Line (Home), Model
    Total for the latest week predict_week.py has produced. Market Line and
    Market Total stay blank/manual -- that's still your sportsbook lookup.
  - Team Profiles: Model Power Rating, SP+ Rating, Off PPA, Def PPA, Talent
    Rank for each of the 10 teams, pulled from the database's latest season.
  - Power Ratings (new tab, created automatically the first time this script
    runs if it doesn't already exist): every FBS team's Dub Gamma
    (Sonny-Moore-seeded) power rating, one column per completed week of the
    season (Week 1 = Moore's own preseason seed, already reflecting week 1
    results -- see gamma_model.py/moore_seed_2026.py -- Week 2 onward is that
    seed replayed forward through gamma_model.py's own 90/10 update formula).
    A new week column is added automatically the first time this script runs
    after that week's games finish -- nothing to do by hand. Also on this
    tab: a small Matchup Spread Calculator (fixed cells to the right of the
    ratings grid) -- type any two team names into the Home Team/Away Team
    cells (there's a dropdown) and Predicted Spread (Home) recomputes live
    in Excel via INDEX/MATCH against the LATEST week column, using the exact
    same formula as gamma_model.predict_spread_home(): away rating - home
    rating - HFA (0 if Neutral Site is set to Y). Your Home Team/Away Team/
    Neutral Site picks are preserved across every re-run -- only the ratings
    grid, the HFA constant display, and the spread formula itself are
    rewritten each time (same "script owns some cells, you own others"
    contract as the Weekly Slate's Market Line column).

A game already on the Weekly Slate (matched by Home + Away team) gets its
Model Line/Model Total updated in place rather than duplicated; a new game
is written into the first empty row.

IMPORTANT: this script does NOT recalculate formulas (that requires
LibreOffice, which this project doesn't assume you have installed). That's
fine in practice -- when you open the file in real Microsoft Excel, it
recalculates every formula automatically on open. You do not need to do
anything extra.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python excel/update_tracker.py
"""
import re
import sys
from pathlib import Path

import duckdb
import openpyxl
import time

from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import DB_PATH, CLEAN_DIR  # noqa: E402
from power_rating import current_ratings  # noqa: E402
import gamma_model  # noqa: E402

TRACKER_PATH = Path(__file__).resolve().parent / "MW_Handicapping_Tracker.xlsx"
WEEKLY_SLATE_ROWS = range(2, 43)   # matches build_tracker.py's layout
TEAM_PROFILE_ROWS = range(2, 12)   # 10 teams

PRED_FILE_RE = re.compile(r"^week_(\d{4})_(\d+)_predictions\.csv$")

# Power Ratings tab -- same small style palette build_tracker.py uses for
# every other tab, duplicated here rather than imported (build_tracker.py is
# a run-once workbook-builder script, not a module -- importing it would
# re-execute the whole thing and overwrite the live tracker).
FONT_NAME = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)
INPUT_FONT = Font(name=FONT_NAME, color="0000FF", size=10)          # blue = user input
FORMULA_FONT = Font(name=FONT_NAME, color="000000", size=10)        # black = formula
FORMULA_FONT_BOLD = Font(name=FONT_NAME, bold=True, color="000000", size=11)
NOTE_FONT = Font(name=FONT_NAME, italic=True, size=9, color="666666")
SUBTITLE_FONT = Font(name=FONT_NAME, bold=True, size=12)

POWER_RATINGS_SHEET = "Power Ratings"
# Matchup calculator lives in fixed columns well to the right of the ratings
# grid (column V/W = 22/23) -- a CFB regular season tops out around 15-16
# weeks, so this leaves plenty of room for the week-by-week grid (Team column
# + one column per week) to grow all season without ever reaching it. Fixed
# columns matter here specifically because Home Team/Away Team/Neutral Site
# are YOUR input cells and must stay put across every re-run.
CALC_LABEL_COL = 22   # V
CALC_VALUE_COL = 23   # W


def latest_predictions_file():
    candidates = []
    for f in CLEAN_DIR.glob("week_*_predictions.csv"):
        m = PRED_FILE_RE.match(f.name)
        if m:
            candidates.append(((int(m.group(1)), int(m.group(2))), f))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[-1][1]


def update_weekly_slate(ws, predictions_path: Path, con):
    import csv
    with open(predictions_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print("Weekly Slate: predictions file is empty -- nothing to write.")
        return

    # CFBD's own betting lines (the same data the website's Matchups page
    # reads) -- auto-fill Market Line/Total when a book has actually posted
    # one for that game yet. A missing game_id here just means no line is in
    # the DB yet (common for games further out); leave that cell for you to
    # fill by hand rather than blanking out anything you already typed in.
    market = {
        game_id: (spread, total)
        for game_id, spread, total in con.execute("""
            SELECT game_id, AVG(spread), AVG(over_under) FROM lines GROUP BY game_id
        """).fetchall()
    }
    filled_market = 0

    # Clear any row for a matchup that ISN'T in this run's predictions --
    # otherwise a game that drops off the list (last week's games once a new
    # week is predicted, or -- what actually happened here -- games that
    # never should've been on this MW-focused sheet in the first place) just
    # sits there forever, since the loop below only ever adds/updates rows,
    # never removes them. A row for a matchup that IS still in the current
    # predictions is left alone here (and updated in place below), so manual
    # Market Line/Total/Notes/Final Dec entries survive a same-week rerun.
    current_keys = {(pred["Home Team"], pred["Away Team"]) for pred in rows}
    cleared = 0
    for r in WEEKLY_SLATE_ROWS:
        home, away = ws[f"D{r}"].value, ws[f"C{r}"].value
        if home and away and (home, away) not in current_keys:
            for col in ("A", "B", "C", "D", "E", "F", "I", "J", "O", "P"):
                ws[f"{col}{r}"] = None
            cleared += 1
    if cleared:
        print(f"Weekly Slate: cleared {cleared} row(s) no longer in this week's predictions")

    # index existing rows by (home, away) so a re-run updates in place
    existing = {}
    first_empty = None
    for r in WEEKLY_SLATE_ROWS:
        home = ws[f"D{r}"].value
        away = ws[f"C{r}"].value
        if home and away:
            existing[(home, away)] = r
        elif first_empty is None:
            first_empty = r

    written = 0
    for pred in rows:
        key = (pred["Home Team"], pred["Away Team"])
        if key in existing:
            r = existing[key]
        elif first_empty is not None:
            r = first_empty
            first_empty = next((row for row in WEEKLY_SLATE_ROWS if row > r and not ws[f"D{row}"].value), None)
        else:
            print(f"  Weekly Slate is full (rows 2-42 all used) -- skipping {key}")
            continue

        ws[f"A{r}"] = int(pred["Week"])
        ws[f"B{r}"] = pred["Date"]
        ws[f"C{r}"] = pred["Away Team"]
        ws[f"D{r}"] = pred["Home Team"]
        ws[f"E{r}"] = float(pred["Model Line (Home)"])
        ws[f"I{r}"] = float(pred["Model Total"])
        written += 1

        # .get() -- older predictions CSVs (from before Game ID was added)
        # simply won't have this column, and that's fine: skip the auto-fill
        # for those rather than erroring.
        game_id = pred.get("Game ID")
        if game_id is not None:
            try:
                spread, total = market.get(int(game_id), (None, None))
            except ValueError:
                spread, total = None, None
            if spread is not None:
                ws[f"F{r}"] = round(spread, 1)
                filled_market += 1
            if total is not None:
                ws[f"J{r}"] = round(total, 1)

    print(f"Weekly Slate: wrote/updated {written} matchup(s)")
    if filled_market:
        print(f"  -> auto-filled Market Line/Total for {filled_market} game(s) with a CFBD line already posted")


def update_team_profiles(ws, con):
    ratings = current_ratings(con)
    sp = dict(con.execute("""
        SELECT team, rating FROM sp_ratings
        WHERE season = (SELECT MAX(season) FROM sp_ratings)
    """).fetchall())
    adv = con.execute("""
        SELECT team, off_ppa, def_ppa FROM advanced_stats
        WHERE season = (SELECT MAX(season) FROM advanced_stats)
    """).fetchall()
    adv_map = {team: (off_ppa, def_ppa) for team, off_ppa, def_ppa in adv}
    rec = dict(con.execute("""
        SELECT team, rank FROM recruiting
        WHERE season = (SELECT MAX(season) FROM recruiting)
    """).fetchall())

    updated = 0
    for r in TEAM_PROFILE_ROWS:
        team = ws[f"A{r}"].value
        if not team:
            continue
        if team in ratings:
            ws[f"D{r}"] = round(ratings[team], 1)
        if team in sp:
            ws[f"E{r}"] = sp[team]
        if team in adv_map:
            off_ppa, def_ppa = adv_map[team]
            ws[f"F{r}"] = off_ppa
            ws[f"G{r}"] = def_ppa
        if team in rec:
            ws[f"H{r}"] = rec[team]
        updated += 1

    print(f"Team Profiles: updated {updated} team row(s)")


def ensure_power_ratings_tab(wb):
    """Creates the Power Ratings tab the first time this script runs against
    a workbook that doesn't have one yet. Returns (worksheet, just_created) --
    `just_created` tells update_power_ratings() below whether it's safe to
    write starting defaults into the Matchup Calculator's input cells (Home
    Team/Away Team/Neutral Site) without clobbering something you've already
    typed in on a prior run."""
    just_created = POWER_RATINGS_SHEET not in wb.sheetnames
    ws = wb[POWER_RATINGS_SHEET] if not just_created else wb.create_sheet(POWER_RATINGS_SHEET)
    if just_created:
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = "B2"
    return ws, just_created


def update_power_ratings(ws, con, just_created):
    """Writes every FBS team's Dub Gamma (Sonny-Moore-seeded) power rating,
    one column per completed week of the season -- see gamma_model.py for
    the model itself. Week 1 is always Moore's own preseason seed (already
    reflecting week 1's results, per moore_seed_2026.py); the replay's own
    update formula only starts applying at week 2 and beyond, so this
    naturally reproduces "weeks 1-4" now and picks up Week 5, 6, ... on its
    own the first time this script runs after each of those weeks finishes
    -- no code change needed as the season progresses.

    Also refreshes the Matchup Spread Calculator's script-owned cells (the
    ratings grid it looks up against, the HFA constant display, and the
    spread formula's own column reference, which has to shift right every
    time a new week column is added) -- but never touches the calculator's
    Home Team/Away Team/Neutral Site input cells once they exist, so your
    picks survive every re-run and just recompute against fresher ratings.
    """
    season = gamma_model.SEED_SEASON
    row = con.execute(
        "SELECT MAX(week) FROM games WHERE season = ? AND completed = TRUE",
        [season],
    ).fetchone()
    max_completed_week = row[0] if row and row[0] else 0
    num_weeks = max(max_completed_week, 1)   # always show at least the Moore seed as "Week 1"

    # One full replay per week shown (through_week=1 hits gamma_model.py's
    # own entering_week>=2 floor and just returns the untouched seed -- see
    # that function's own docstring) -- cheap (a linear pass over a few
    # hundred games each), same cost profile replay_ratings() already has
    # everywhere else it's called.
    weekly_ratings = [gamma_model.replay_ratings(con, season=season, through_week=wk)[0]
                      for wk in range(1, num_weeks + 1)]
    latest_ratings = weekly_ratings[-1]

    team_list = sorted(
        gamma_model.SEED_RATINGS.keys(),
        key=lambda t: latest_ratings.get(t, gamma_model.DEFAULT_SEED_RATING),
        reverse=True,
    )
    last_row = 1 + len(team_list)
    last_week_col = get_column_letter(1 + num_weeks)

    # --- ratings grid (script-owned in full -- rewritten every run) ---
    ws["A1"] = "Team"
    ws["A1"].font = HEADER_FONT
    ws["A1"].fill = HEADER_FILL
    for wk in range(1, num_weeks + 1):
        col = get_column_letter(1 + wk)
        cell = ws[f"{col}1"]
        cell.value = f"Week {wk}"
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")

    for idx, team in enumerate(team_list):
        r = 2 + idx
        ws[f"A{r}"] = team
        ws[f"A{r}"].font = FORMULA_FONT
        for wk in range(1, num_weeks + 1):
            col = get_column_letter(1 + wk)
            val = weekly_ratings[wk - 1].get(team)
            cell = ws[f"{col}{r}"]
            cell.value = round(val, 1) if val is not None else None
            cell.font = FORMULA_FONT

    ws.column_dimensions["A"].width = 22
    for wk in range(1, num_weeks + 1):
        ws.column_dimensions[get_column_letter(1 + wk)].width = 10

    # --- Matchup Spread Calculator (fixed columns -- see CALC_LABEL_COL) ---
    label_col = get_column_letter(CALC_LABEL_COL)
    value_col = get_column_letter(CALC_VALUE_COL)

    ws[f"{label_col}1"] = "Matchup Spread Calculator"
    ws[f"{label_col}1"].font = SUBTITLE_FONT

    labels = {
        3: "Home Team",
        4: "Away Team",
        5: "Neutral Site? (Y/N)",
        7: "HFA (model constant)",
        9: "Predicted Spread (Home)",
    }
    for r, text in labels.items():
        cell = ws[f"{label_col}{r}"]
        cell.value = text
        cell.font = FORMULA_FONT_BOLD if r == 9 else FORMULA_FONT

    # Script-owned: HFA display and the spread formula (its column reference
    # into the ratings grid has to move every time a week column is added).
    ws[f"{value_col}7"] = gamma_model.HFA
    ws[f"{value_col}7"].font = FORMULA_FONT
    spread_formula = (
        f'=IF(OR({value_col}3="",{value_col}4=""),"",'
        f'INDEX(${last_week_col}$2:${last_week_col}${last_row},MATCH({value_col}4,$A$2:$A${last_row},0))'
        f'-INDEX(${last_week_col}$2:${last_week_col}${last_row},MATCH({value_col}3,$A$2:$A${last_row},0))'
        f'-IF(UPPER({value_col}5)="Y",0,{value_col}7))'
    )
    ws[f"{value_col}9"] = spread_formula
    ws[f"{value_col}9"].font = FORMULA_FONT_BOLD
    ws[f"{label_col}10"] = "(negative = home team favored, e.g. \"Home -6.5\")"
    ws[f"{label_col}10"].font = NOTE_FONT

    ws.column_dimensions[label_col].width = 24
    ws.column_dimensions[value_col].width = 16

    # User-owned: only set once, the first time this tab is created, and
    # never touched again -- your picks on later runs are left exactly as
    # you left them.
    if just_created:
        ws[f"{value_col}3"] = ""
        ws[f"{value_col}3"].font = INPUT_FONT
        ws[f"{value_col}4"] = ""
        ws[f"{value_col}4"].font = INPUT_FONT
        ws[f"{value_col}5"] = "N"
        ws[f"{value_col}5"].font = INPUT_FONT

        team_range = f"=$A$2:$A${last_row}"
        dv_home = DataValidation(type="list", formula1=team_range, allow_blank=True)
        dv_away = DataValidation(type="list", formula1=team_range, allow_blank=True)
        ws.add_data_validation(dv_home)
        ws.add_data_validation(dv_away)
        dv_home.add(f"{value_col}3")
        dv_away.add(f"{value_col}4")

    print(f"Power Ratings: {len(team_list)} team(s) x {num_weeks} week(s) "
          f"(through week {num_weeks}){' -- tab created' if just_created else ''}")


def main():
    if not TRACKER_PATH.exists():
        print(f"Tracker workbook not found at {TRACKER_PATH}. Run excel/build_tracker.py first, "
              "or move MW_Handicapping_Tracker.xlsx next to this script.")
        return

    pred_file = latest_predictions_file()
    if pred_file is None:
        print("No predictions CSV found in data/clean/. Run src/predict_week.py first.")
        return
    print(f"Using predictions from {pred_file.name}")

    wb = openpyxl.load_workbook(TRACKER_PATH)  # formulas preserved as formulas, not evaluated

    con = duckdb.connect(str(DB_PATH))
    update_weekly_slate(wb["Weekly Slate"], pred_file, con)
    update_team_profiles(wb["Team Profiles"], con)
    pr_ws, pr_just_created = ensure_power_ratings_tab(wb)
    update_power_ratings(pr_ws, con, pr_just_created)
    con.close()

    wb.save(TRACKER_PATH)
    print(f"\nSaved {TRACKER_PATH}. Open it in Excel -- formulas recalculate automatically on open.")


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
