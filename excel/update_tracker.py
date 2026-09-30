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
  - MW Team ATS (new tab, created automatically the first time this script
    runs if it doesn't already exist): every 2026 Mountain West team's
    against-the-spread record for the CURRENT SEASON ONLY, graded against
    the real market line (data/clean via the `lines` table's own
    AVG(spread) per game -- same source Weekly Slate's Market Line auto-fill
    already uses), NOT any model's pick -- this is purely "did the team
    itself beat the number," independent of Ridge/Dub Beta/Dub Gamma
    entirely. A season-record summary (Cover-Loss-Push, cover %) sits at the
    top, and a full game-by-game log (week, opponent, home/away, that team's
    own spread, result, cover margin) sits below it. A game with no market
    line on file yet is left out of the log rather than guessed at. Fully
    regenerated every run, same as Power Ratings -- there's nothing here
    for you to type in, so nothing needs to be preserved across re-runs.
  - Strength of Record (new tab): every FBS team's SOR (wins above an
    exactly-average team's expected record against that same schedule),
    current season only, next to its Elo rating and a Rank Gap column
    flagging a big Elo-vs-SOR disagreement. See update_sor_tab()'s own
    docstring. Fully regenerated every run, same as MW Team ATS.
  - National Slate (new tab): the all-FBS counterpart to Weekly Slate --
    every FBS game this week (not just Mountain West), Gamma's live spread,
    Ridge/Massey as informational candidates, the real market line, the
    edge between them (green-filled when it's a real, threshold-clearing
    lean -- same green/backtest.EDGE_THRESHOLD as MW Team ATS's Cover fill),
    and a straight-up Win/Loss grade once a game's final. Sorted by
    conference with AutoFilter on, so you can drop it down to one
    conference at a time. See _compute_national_slate_rows()'s own
    docstring for why XGBoost isn't on this one. Fully regenerated every run.
  - One "<Conference> Slate" tab per FBS conference (ACC Slate, Big Ten
    Slate, ... Mountain West Slate, 11 tabs total, in addition to keeping
    National Slate): a standings block (each team's conference and overall
    win-loss record, sorted by conference record) on top, then that
    conference's own slice of the exact same National Slate rows below it
    (a non-conference game involving two different conferences' teams shows
    up on both conferences' tabs). See update_conference_slate()'s own
    docstring. Fully regenerated every run, same as National Slate.

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
from power_rating import current_ratings, current_sor  # noqa: E402
import gamma_model  # noqa: E402
import model  # noqa: E402
import massey_model  # noqa: E402
import totals_model  # noqa: E402
import backtest  # noqa: E402
from predict_week import auto_detect_week  # noqa: E402
from teams import MW_TEAMS_2026, FBS_CONFERENCES, normalize_team_name  # noqa: E402

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

# MW Team ATS tab
MW_ATS_SHEET = "MW Team ATS"
CURRENT_SEASON = gamma_model.SEED_SEASON   # 2026 -- same season constant every other tab uses
COVER_FILL = PatternFill("solid", fgColor="C6EFCE")     # light green
NO_COVER_FILL = PatternFill("solid", fgColor="FFC7CE")  # light red
PUSH_FILL = PatternFill("solid", fgColor="E7E6E6")      # light gray

# Strength of Record tab
SOR_SHEET = "Strength of Record"
RANK_GAP_FILL = PatternFill("solid", fgColor="FFEB9C")  # light amber -- flags a big Elo/SOR disagreement

# National Slate tab -- the Excel-side counterpart to the site's planned
# conference-by-conference Matchups/Predictions expansion (see this
# project's own site conversation): every FBS game this week, not just
# Mountain West, with the live model's (Gamma's) line, two cheap
# informational candidate lines (Ridge, Massey), the real market line, and
# the edge between them -- green-filled exactly like MW Team ATS's Cover
# fill above when a game clears EDGE_THRESHOLD, i.e. a real recommended
# lean, not just noise. Reuses backtest.EDGE_THRESHOLD (2.0 pts) rather
# than a second copy of that number, so "what counts as an edge" never
# drifts between this tab and the site/backtest's own grading.
NATIONAL_SLATE_SHEET = "National Slate"
EDGE_FILL = COVER_FILL          # same light green -- "real edge" reuses the same visual language as "covered"
CORRECT_FILL = COVER_FILL       # Model Result: Win -- reuses the same green rather than inventing a third color
INCORRECT_FILL = NO_COVER_FILL  # Model Result: Loss


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

    # No frozen panes anywhere in this workbook (Cole's own call) -- this
    # tab is never recreated by this script (only build_tracker.py's
    # one-time setup makes it), so a workbook built before this change keeps
    # whatever build_tracker.py set unless it's explicitly cleared here on
    # every run.
    ws.freeze_panes = None

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

    # No frozen panes anywhere in this workbook (Cole's own call) -- same
    # "reset on every run, not just at creation" fix as Weekly Slate above.
    ws.freeze_panes = None

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
    # No frozen panes anywhere in this workbook (Cole's own call) -- reset
    # unconditionally, not just on creation, so a workbook that already has
    # one frozen from before this change gets it cleared on the next run too.
    ws.freeze_panes = None
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


def ensure_mw_ats_tab(wb):
    """Creates the MW Team ATS tab the first time this script runs against a
    workbook that doesn't have one yet. Unlike Power Ratings, nothing on this
    tab is ever typed in by hand -- it's fully regenerated from the database
    every run -- so on a later run this just wipes the sheet clean first
    rather than needing any "preserve the user's cells" logic."""
    if MW_ATS_SHEET not in wb.sheetnames:
        ws = wb.create_sheet(MW_ATS_SHEET)
        ws.sheet_view.showGridLines = False
    else:
        ws = wb[MW_ATS_SHEET]
        ws.delete_rows(1, ws.max_row)
    return ws


def update_mw_ats(ws, con):
    """Every 2026 MW team's against-the-spread record, CURRENT SEASON ONLY,
    graded against the real market line (the `lines` table -- same source
    Weekly Slate's Market Line auto-fill already uses), independent of any
    model's own pick. A season-record summary sits at the top; a full
    game-by-game log sits below it. See this file's own module docstring.
    """
    games = con.execute("""
        SELECT game_id, week, start_date, home_team, away_team, home_points, away_points
        FROM games
        WHERE season = ? AND completed = TRUE
        ORDER BY week, start_date
    """, [CURRENT_SEASON]).fetchall()

    market = dict(con.execute("SELECT game_id, AVG(spread) FROM lines GROUP BY game_id").fetchall())

    # One row per (team, game) actually played, from that team's own
    # perspective -- team_spread negative = that team favored, team_margin =
    # that team's own points minus the opponent's. A game with no market
    # line yet is left out entirely rather than guessed at (same convention
    # Weekly Slate's own Market Line auto-fill already uses).
    per_team_games = {team: [] for team in MW_TEAMS_2026}
    for game_id, week, start_date, home_team, away_team, home_pts, away_pts in games:
        if home_pts is None or away_pts is None:
            continue
        market_spread_home = market.get(game_id)
        if market_spread_home is None:
            continue
        home_team = normalize_team_name(home_team)
        away_team = normalize_team_name(away_team)
        home_margin = home_pts - away_pts

        if home_team in per_team_games:
            per_team_games[home_team].append({
                "week": week, "date": str(start_date)[:10], "opponent": away_team,
                "site": "Home", "team_spread": market_spread_home, "team_margin": home_margin,
            })
        if away_team in per_team_games:
            per_team_games[away_team].append({
                "week": week, "date": str(start_date)[:10], "opponent": home_team,
                "site": "Away", "team_spread": -market_spread_home, "team_margin": -home_margin,
            })

    def grade(g):
        cover_margin = g["team_margin"] + g["team_spread"]
        if abs(cover_margin) < 1e-6:
            return "Push", cover_margin
        return ("Cover" if cover_margin > 0 else "No Cover"), cover_margin

    team_order = sorted(MW_TEAMS_2026)

    # --- summary block: Team | ATS Record (Cover-Loss-Push) | Cover % ---
    ws["A1"] = f"MW Team ATS -- {CURRENT_SEASON} Season (graded vs. the real market line, not any model)"
    ws["A1"].font = SUBTITLE_FONT

    summary_header_row = 3
    for c, h in enumerate(["Team", "ATS Record (C-L-P)", "Cover %"], start=1):
        cell = ws.cell(row=summary_header_row, column=c, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center")

    for i, team in enumerate(team_order):
        covers = sum(1 for g in per_team_games[team] if grade(g)[0] == "Cover")
        losses = sum(1 for g in per_team_games[team] if grade(g)[0] == "No Cover")
        pushes = sum(1 for g in per_team_games[team] if grade(g)[0] == "Push")
        graded = covers + losses

        r = summary_header_row + 1 + i
        ws.cell(row=r, column=1, value=team).font = FORMULA_FONT
        ws.cell(row=r, column=2, value=f"{covers}-{losses}-{pushes}").font = FORMULA_FONT
        pct_cell = ws.cell(row=r, column=3, value=(round(covers / graded, 3) if graded else None))
        pct_cell.font = FORMULA_FONT
        if graded:
            pct_cell.number_format = "0.0%"

    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 20
    ws.column_dimensions["C"].width = 10

    # --- full game-by-game log, grouped by team (alphabetical), then week ---
    log_header_row = summary_header_row + len(team_order) + 3
    log_headers = ["Team", "Week", "Date", "Opponent", "Site", "Team's Spread",
                   "Result (Margin)", "ATS Result", "Cover Margin"]
    for c, h in enumerate(log_headers, start=1):
        cell = ws.cell(row=log_header_row, column=c, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    fill_by_result = {"Cover": COVER_FILL, "No Cover": NO_COVER_FILL, "Push": PUSH_FILL}
    r = log_header_row + 1
    total_graded = 0
    for team in team_order:
        for g in sorted(per_team_games[team], key=lambda g: g["week"]):
            result, cover_margin = grade(g)
            values = [team, g["week"], g["date"], g["opponent"], g["site"],
                      round(g["team_spread"], 1), int(g["team_margin"]), result, round(cover_margin, 1)]
            for c, val in enumerate(values, start=1):
                ws.cell(row=r, column=c, value=val).font = FORMULA_FONT
            ws.cell(row=r, column=8).fill = fill_by_result[result]
            r += 1
            total_graded += 1

    for col, width in zip("ABCDEFGHI", [16, 6, 12, 16, 7, 13, 15, 11, 13]):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = None  # no frozen panes anywhere in this workbook -- Cole's own call

    print(f"MW Team ATS: {len(team_order)} team(s), {total_graded} graded team-game(s) this season")


def ensure_sor_tab(wb):
    """Creates the Strength of Record tab the first time this script runs
    against a workbook that doesn't have one yet. Same fully-regenerated,
    nothing-typed-in-by-hand pattern as MW Team ATS -- see that function's
    own docstring."""
    if SOR_SHEET not in wb.sheetnames:
        ws = wb.create_sheet(SOR_SHEET)
        ws.sheet_view.showGridLines = False
    else:
        ws = wb[SOR_SHEET]
        ws.delete_rows(1, ws.max_row)
    ws.freeze_panes = None  # no frozen panes anywhere in this workbook -- Cole's own call
    return ws


def update_sor_tab(ws, con):
    """
    Strength of Record, current season only (see sor_baseline's own comment
    in schema.sql: SOR resets to 0.0 every season, so a cross-season table
    wouldn't mean anything). SOR answers "how many wins above what an
    exactly-average FBS team would've earned against this same schedule" --
    a resume metric, separate from the Elo rating's own "how good is this
    team" process estimate (see power_rating.py's run_ratings() docstring).

    The whole point of putting this next to the Elo rating here, rather
    than as its own siloed tab, is the Rank Gap column: Elo Rank minus SOR
    Rank. A big POSITIVE gap means a team's raw Elo number (no preseason
    prior, so it leans entirely on this season's results) is running well
    ahead of what its actual resume supports -- exactly the "is this
    team's power rating too high" question this tab exists to answer at a
    glance, for every team, every week, instead of investigating one team
    at a time by hand.
    """
    season = CURRENT_SEASON
    ratings = current_ratings(con)
    sor = current_sor(con)

    # This season's W-L, FBS opponents only (same scope as SOR itself) --
    # for context alongside the SOR number, not used in any calculation.
    games = con.execute("""
        SELECT home_team, away_team, home_points, away_points, home_conference, away_conference
        FROM games
        WHERE season = ? AND completed = TRUE AND home_points IS NOT NULL AND away_points IS NOT NULL
    """, [season]).fetchall()
    record = {}
    for home, away, hp, ap, home_conf, away_conf in games:
        if hp == ap:
            continue
        home_win = hp > ap
        if away_conf in FBS_CONFERENCES:
            w, l = record.get(home, (0, 0))
            record[home] = (w + 1, l) if home_win else (w, l + 1)
        if home_conf in FBS_CONFERENCES:
            w, l = record.get(away, (0, 0))
            record[away] = (w + 1, l) if not home_win else (w, l + 1)

    # Every team that's played an FBS game this season -- SOR/record scope,
    # not gamma_model.SEED_RATINGS' full offseason roster (a team with no
    # games yet has nothing to rank here).
    team_list = sorted(sor.keys(), key=lambda t: -sor.get(t, 0.0))
    elo_rank = {t: i + 1 for i, (t, _) in enumerate(sorted(ratings.items(), key=lambda kv: -kv[1]))}
    sor_rank = {t: i + 1 for i, t in enumerate(team_list)}

    ws["A1"] = f"Strength of Record -- {season} Season (resets every season -- see column notes below)"
    ws["A1"].font = SUBTITLE_FONT
    ws["A3"] = ("SOR = wins above what an exactly-average FBS team would've earned against this exact "
                "schedule. Rank Gap = Elo Rank minus SOR Rank -- a big positive number means the Elo "
                "rating (no preseason blend, so it's all this season's results) is running ahead of "
                "what the resume actually supports.")
    ws["A3"].font = NOTE_FONT

    header_row = 5
    headers = ["Team", "W-L (FBS)", "SOR", "SOR Rank", "Elo Rating", "Elo Rank", "Rank Gap (Elo - SOR)"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=c, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    for i, team in enumerate(team_list):
        r = header_row + 1 + i
        w, l = record.get(team, (0, 0))
        gap = elo_rank.get(team, 0) - sor_rank.get(team, 0)

        ws.cell(row=r, column=1, value=team).font = FORMULA_FONT
        ws.cell(row=r, column=2, value=f"{w}-{l}").font = FORMULA_FONT
        ws.cell(row=r, column=3, value=round(sor.get(team, 0.0), 2)).font = FORMULA_FONT
        ws.cell(row=r, column=4, value=sor_rank.get(team)).font = FORMULA_FONT
        rating_cell = ws.cell(row=r, column=5, value=round(ratings[team], 1) if team in ratings else None)
        rating_cell.font = FORMULA_FONT
        ws.cell(row=r, column=6, value=elo_rank.get(team)).font = FORMULA_FONT
        gap_cell = ws.cell(row=r, column=7, value=gap)
        gap_cell.font = FORMULA_FONT
        if gap >= 15:   # arbitrary but generous -- flags only a real, sizable disagreement
            gap_cell.fill = RANK_GAP_FILL

    for col, width in zip("ABCDEFG", [22, 10, 8, 10, 11, 10, 18]):
        ws.column_dimensions[col].width = width

    print(f"Strength of Record: {len(team_list)} team(s) ranked for {season}")


def ensure_national_slate_tab(wb):
    """Creates the National Slate tab the first time this script runs against
    a workbook that doesn't have one yet. Same fully-regenerated,
    nothing-typed-in-by-hand pattern as MW Team ATS/Strength of Record above
    -- there's nothing here for you to type in, so a re-run just wipes the
    sheet and rebuilds it from the database."""
    if NATIONAL_SLATE_SHEET not in wb.sheetnames:
        ws = wb.create_sheet(NATIONAL_SLATE_SHEET)
        ws.sheet_view.showGridLines = False
    else:
        ws = wb[NATIONAL_SLATE_SHEET]
        ws.delete_rows(1, ws.max_row)
    return ws


def _compute_national_slate_rows(con):
    """
    Every FBS game in the current auto-detected week -- not just Mountain
    West (that's still Weekly Slate's own job, unchanged) -- with the live
    model's (Gamma's) spread, two cheap informational candidates (Ridge,
    Massey -- same "never the live pick" treatment they get everywhere else
    in this project), the real market line, and the edge between them.

    Returns (season, week, rows) -- rows is the list of dicts _write_slate_
    table() renders. Returns (None, None, []) if no upcoming week is found
    at all, or (season, week, []) if one was found but had no FBS games.
    Factored out from update_national_slate() (now just a thin writer, see
    below) so the ~55-65 non-MW games' worth of model refitting (Ridge,
    totals) this does happens exactly ONCE per pipeline run and gets reused
    by every one of the 11 per-conference tabs (see update_conference_slate())
    rather than being recomputed -- and re-paid for -- 11 more times.

    XGBoost is deliberately left OUT of this tab. predict_week.py only pays
    for its hyperparameter search once, for the ~6-8 MW games on Weekly
    Slate; refitting it again here for the ~55-65 additional national games
    every week would meaningfully slow down every pipeline run for a line
    that's informational-only everywhere it already appears. Say the word
    if you want it here too and I'll wire it in, accepting that cost.

    Spread Edge/Total Edge are meant to be green-filled (EDGE_FILL, the same
    green MW Team ATS uses for a Cover) whenever they clear
    backtest.EDGE_THRESHOLD -- a real, threshold-clearing lean, not just
    noise, same definition the site/backtest.py's own grading uses (imported,
    not re-typed, so the two can't silently drift apart) -- see
    _write_slate_table() for where that fill actually gets applied. Once a
    game is final, Model Result shows whether Gamma's own favored side
    actually won straight-up -- green for a win, red for a loss, blank for a
    push/pick'em with no favorite to grade. This is a straight-up grade (did
    the favored side win outright), not an against-the-spread grade --
    Weekly Slate/MW Team ATS already own the ATS side of this for Mountain
    West specifically; this data's job is the national spread-prediction
    picture, not a second copy of the ATS tracker.
    """
    detected = auto_detect_week(con)
    if detected is None:
        return None, None, []
    season, week = detected[0], detected[1]

    # This week's games nationally, with each side's real CFBD conference --
    # same FBS_CONFERENCES allowlist used everywhere else in this project
    # (see teams.py's own comment) so a stray FCS game never sneaks onto a
    # tab meant to be "every FBS game."
    game_conf = {
        game_id: (home_conf, away_conf)
        for game_id, home_conf, away_conf in con.execute("""
            SELECT game_id, home_conference, away_conference
            FROM games WHERE season = ? AND week = ?
        """, [season, week]).fetchall()
    }
    game_result = {
        game_id: (completed, home_pts, away_pts)
        for game_id, completed, home_pts, away_pts in con.execute("""
            SELECT game_id, completed, home_points, away_points
            FROM games WHERE season = ? AND week = ?
        """, [season, week]).fetchall()
    }

    upcoming = model.load_upcoming_frame(con, season, week)
    upcoming = upcoming[upcoming["game_id"].isin(game_conf.keys())].copy()
    upcoming = upcoming[upcoming.apply(
        lambda r: game_conf[r["game_id"]][0] in FBS_CONFERENCES
        or game_conf[r["game_id"]][1] in FBS_CONFERENCES, axis=1,
    )].reset_index(drop=True)
    if upcoming.empty:
        return season, week, []

    market = {
        game_id: (spread, total)
        for game_id, spread, total in con.execute(
            "SELECT game_id, AVG(spread), AVG(over_under) FROM lines GROUP BY game_id"
        ).fetchall()
    }

    # Gamma (live model -- see gamma_model.py) and Ridge (cheap, no search --
    # see model.py) are refit/replayed fresh here rather than read from
    # predict_week.py's CSV, same reasoning export_site_data.py already
    # documents for its own predictions: it decouples this tab from that
    # CSV's own MW-only scope and staleness, at negligible extra cost for
    # these two specifically (unlike XGBoost -- see this function's own
    # docstring on why that one stays out).
    train_df = model.load_training_frame(con)
    gamma_ratings, gamma_warned = gamma_model.replay_ratings(con)
    if gamma_warned:
        print(f"  [National Slate] Dub Gamma: {len(gamma_warned)} team(s) had no Moore seed rating, "
              f"defaulted to {gamma_model.DEFAULT_SEED_RATING:.2f}: {gamma_warned}")
    massey_ratings = massey_model.ratings_entering_week(con, season, week)

    ridge_spread_home = {}
    if len(train_df) >= 10:
        pipe, _ = model.fit_margin_model(train_df)
        ridge_margin = model.predict_margin(pipe, upcoming)
        ridge_spread_home = dict(zip(upcoming["game_id"].astype(int), -ridge_margin))

    totals_train = totals_model.load_totals_training_frame(con)
    total_pipe, _ = totals_model.fit_total_model(totals_train)
    wk_totals_features = totals_model.load_upcoming_totals_frame(con, season, week)
    total_map = {}
    if not wk_totals_features.empty:
        total_preds = totals_model.predict_total(total_pipe, wk_totals_features)
        total_map = dict(zip(wk_totals_features["game_id"].astype(int), total_preds))

    rows = []
    for row in upcoming.itertuples():
        gid = int(row.game_id)
        home_conf, away_conf = game_conf.get(gid, (None, None))
        market_spread_home, market_total = market.get(gid, (None, None))

        gamma_spread = gamma_model.predict_spread_home(
            gamma_ratings, row.home_team, row.away_team, neutral_site=bool(row.neutral_site))
        ridge_spread = ridge_spread_home.get(gid)
        massey_spread = (
            massey_model.predict_spread_home(massey_ratings, row.home_team, row.away_team,
                                              neutral_site=bool(row.neutral_site))
            if massey_ratings else None
        )
        model_total = total_map.get(gid)

        spread_edge = (market_spread_home - gamma_spread) if market_spread_home is not None else None
        total_edge = (model_total - market_total) if (model_total is not None and market_total is not None) else None

        completed, home_pts, away_pts = game_result.get(gid, (False, None, None))
        model_result = None
        if completed and home_pts is not None and away_pts is not None:
            actual_margin = home_pts - away_pts
            gamma_margin = -gamma_spread
            if gamma_margin != 0 and actual_margin != 0:
                model_result = "Win" if (actual_margin > 0) == (gamma_margin > 0) else "Loss"

        rows.append({
            "week": int(row.week), "date": str(row.start_date)[:10],
            "home_conf": home_conf or "", "away_conf": away_conf or "",
            "away_team": row.away_team, "home_team": row.home_team,
            "gamma_spread": round(gamma_spread, 1),
            "market_spread": round(market_spread_home, 1) if market_spread_home is not None else None,
            "spread_edge": round(spread_edge, 1) if spread_edge is not None else None,
            "ridge_spread": round(ridge_spread, 1) if ridge_spread is not None else None,
            "massey_spread": round(massey_spread, 1) if massey_spread is not None else None,
            "model_total": round(model_total, 1) if model_total is not None else None,
            "market_total": round(market_total, 1) if market_total is not None else None,
            "total_edge": round(total_edge, 1) if total_edge is not None else None,
            "away_pts": away_pts if completed else None,
            "home_pts": home_pts if completed else None,
            "model_result": model_result,
        })

    rows.sort(key=lambda r: (r["home_conf"], r["date"], r["home_team"]))
    return season, week, rows


_SLATE_HEADERS = [
    "Week", "Date", "Home Conf", "Away Conf", "Away Team", "Home Team",
    "Model Line (Home)", "Market Line (Home)", "Spread Edge (pts)",
    "Ridge Line (Home)", "Massey Line (Home)",
    "Model Total", "Market Total", "Total Edge (pts)",
    "Away Pts", "Home Pts", "Model Result",
]
_SLATE_COL_WIDTHS = [6, 11, 12, 12, 15, 15, 16, 16, 14, 15, 15, 11, 11, 12, 9, 9, 12]


def _write_slate_table(ws, rows, start_row=1, freeze=True):
    """
    Writes the 17-column slate table (same columns/fills National Slate has
    always used) starting at `start_row` -- start_row > 1 is what lets a
    per-conference tab (see update_conference_slate()) put a standings block
    above this same table on one sheet. Returns the last row written
    (the header row if `rows` is empty).

    `freeze` is accepted for backward compatibility with every call site but
    no longer does anything -- Cole asked for NO frozen panes anywhere in
    this workbook at all (freezing the per-conference tabs' standings+header
    block used to eat most of a normal Excel window on a small conference,
    which read as "I can't scroll" -- see git history for that first fix;
    rather than re-tuning where freezing helps vs. hurts per tab, every tab
    now just scrolls normally, full stop).
    """
    header_row = start_row
    for c, h in enumerate(_SLATE_HEADERS, start=1):
        cell = ws.cell(row=header_row, column=c, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True)

    for i, r in enumerate(rows, start=header_row + 1):
        values = [
            r["week"], r["date"], r["home_conf"], r["away_conf"], r["away_team"], r["home_team"],
            r["gamma_spread"], r["market_spread"], r["spread_edge"],
            r["ridge_spread"], r["massey_spread"],
            r["model_total"], r["market_total"], r["total_edge"],
            r["away_pts"], r["home_pts"], r["model_result"],
        ]
        for c, val in enumerate(values, start=1):
            ws.cell(row=i, column=c, value=val).font = FORMULA_FONT

        edge_cell = ws.cell(row=i, column=9)
        if r["spread_edge"] is not None and abs(r["spread_edge"]) >= backtest.EDGE_THRESHOLD:
            edge_cell.fill = EDGE_FILL
        total_edge_cell = ws.cell(row=i, column=14)
        if r["total_edge"] is not None and abs(r["total_edge"]) >= backtest.EDGE_THRESHOLD:
            total_edge_cell.fill = EDGE_FILL

        result_cell = ws.cell(row=i, column=17)
        if r["model_result"] == "Win":
            result_cell.fill = CORRECT_FILL
        elif r["model_result"] == "Loss":
            result_cell.fill = INCORRECT_FILL

    last_row = header_row + len(rows)
    ws.auto_filter.ref = f"A{header_row}:Q{last_row}"
    for col, width in zip("ABCDEFGHIJKLMNOPQ", _SLATE_COL_WIDTHS):
        ws.column_dimensions[col].width = width
    # Always cleared, never set -- see this function's own docstring.
    # Explicit (not just "never assigned") so a workbook that already has a
    # frozen pane from before this change gets it cleared on the next run,
    # the same "reset stale state" fix update_conference_slate() already
    # does for freeze_panes elsewhere.
    ws.freeze_panes = None
    return last_row


def render_national_slate(ws, season, week, rows):
    """Thin writer -- all the computing already happened in
    _compute_national_slate_rows(), this just renders it onto the tab."""
    if season is None:
        ws["A1"] = "No upcoming (incomplete) games found in the DB -- run the pull scripts first."
        ws["A1"].font = NOTE_FONT
        print("National Slate: no upcoming week detected -- left a placeholder note.")
        return
    if not rows:
        ws["A1"] = f"No FBS games found for season {season}, week {week}."
        ws["A1"].font = NOTE_FONT
        print(f"National Slate: no FBS games for season {season} week {week}.")
        return

    _write_slate_table(ws, rows, start_row=1)
    n_edges = sum(1 for r in rows if r["spread_edge"] is not None and abs(r["spread_edge"]) >= backtest.EDGE_THRESHOLD)
    print(f"National Slate: {len(rows)} FBS game(s) for season {season} week {week} "
          f"({n_edges} with a real spread edge >= {backtest.EDGE_THRESHOLD} pts)")


STANDINGS_HEADER_FILL = PatternFill("solid", fgColor="2F5233")  # dark green -- visually distinct from the slate's navy header
STANDINGS_HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)


def _conference_standings(con, conference, season):
    """
    Win-loss standings for every team that lined up as `conference` (home or
    away) at least once this season, per CFBD's own home_conference/
    away_conference columns on `games` -- same source of truth
    export_site_data.py's team_conference_record()/build_matchup_grid() use,
    so a team's conference membership here can never drift from what the
    site itself shows. Both conference-only (games.conference_game = TRUE,
    same definition team_conference_record() uses) and overall records are
    computed, current season only. Sorted by conference win percentage (ties
    broken by conference wins, then overall win percentage) -- the standard
    "who's actually leading the conference" order.
    """
    teams = sorted({
        t for (t,) in con.execute("""
            SELECT home_team FROM games WHERE season = ? AND home_conference = ?
            UNION
            SELECT away_team FROM games WHERE season = ? AND away_conference = ?
        """, [season, conference, season, conference]).fetchall()
    })

    rows = []
    for team in teams:
        overall = con.execute("""
            SELECT
                SUM(CASE WHEN home_team = ? AND home_points > away_points THEN 1
                         WHEN away_team = ? AND away_points > home_points THEN 1 ELSE 0 END),
                SUM(CASE WHEN (home_team = ? AND home_points < away_points)
                           OR (away_team = ? AND away_points < home_points) THEN 1 ELSE 0 END)
            FROM games
            WHERE completed = TRUE AND (home_team = ? OR away_team = ?) AND season = ?
        """, [team, team, team, team, team, team, season]).fetchone()
        conf = con.execute("""
            SELECT
                SUM(CASE WHEN home_team = ? AND home_points > away_points THEN 1
                         WHEN away_team = ? AND away_points > home_points THEN 1 ELSE 0 END),
                SUM(CASE WHEN (home_team = ? AND home_points < away_points)
                           OR (away_team = ? AND away_points < home_points) THEN 1 ELSE 0 END)
            FROM games
            WHERE completed = TRUE AND conference_game = TRUE
              AND (home_team = ? OR away_team = ?) AND season = ?
        """, [team, team, team, team, team, team, season]).fetchone()
        ow, ol = overall[0] or 0, overall[1] or 0
        cw, cl = conf[0] or 0, conf[1] or 0
        rows.append({"team": team, "conf_wins": cw, "conf_losses": cl,
                      "overall_wins": ow, "overall_losses": ol})

    def _pct(w, l):
        return w / (w + l) if (w + l) else 0.0

    rows.sort(key=lambda r: (-_pct(r["conf_wins"], r["conf_losses"]), -r["conf_wins"],
                              -_pct(r["overall_wins"], r["overall_losses"])))
    return rows


def _write_standings_block(ws, conference, standings, start_row=1):
    """
    Small W-L table: Team, Conf W, Conf L, Conf Pct, Overall W, Overall L.
    Returns the row number the caller should start the slate table at (one
    blank row below the last standings row)."""
    ws.cell(row=start_row, column=1, value=f"{conference} Standings").font = SUBTITLE_FONT
    header_row = start_row + 1
    headers = ["Team", "Conf W", "Conf L", "Conf Pct", "Overall W", "Overall L"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=c, value=h)
        cell.font = STANDINGS_HEADER_FONT
        cell.fill = STANDINGS_HEADER_FILL
        cell.alignment = Alignment(horizontal="center")

    r = header_row + 1
    for s in standings:
        cw, cl = s["conf_wins"], s["conf_losses"]
        pct = round(cw / (cw + cl), 3) if (cw + cl) else None
        values = [s["team"], cw, cl, pct, s["overall_wins"], s["overall_losses"]]
        for c, val in enumerate(values, start=1):
            ws.cell(row=r, column=c, value=val).font = FORMULA_FONT
        r += 1

    return r + 1  # one blank row, then the slate table starts here


def ensure_conference_slate_tab(wb, conference):
    """Create-or-clear pattern, same as National Slate/MW Team ATS/SOR --
    one tab per FBS conference (e.g. "Mountain West Slate")."""
    sheet_name = f"{conference} Slate"
    if sheet_name not in wb.sheetnames:
        ws = wb.create_sheet(sheet_name)
        ws.sheet_view.showGridLines = False
    else:
        ws = wb[sheet_name]
        ws.delete_rows(1, ws.max_row)
    return ws


def update_conference_slate(ws, con, conference, season, week, all_rows):
    """
    Standings block on top (see _conference_standings()), then this
    conference's own slice of the already-computed National Slate rows
    below it -- a non-conference game (e.g. a Mountain West team @ a Big Ten
    team) shows up on BOTH conferences' tabs, same as it would on either
    conference's real schedule. Reuses `all_rows` as computed once in
    main() rather than recomputing anything per conference, so a game here
    can never show different numbers than the same game on National Slate.
    """
    # Standings reflect completed games for CURRENT_SEASON regardless of
    # whether an upcoming week was detected -- a None `season` here would
    # just mean "no upcoming week to build a slate for," not "no season."
    standings = _conference_standings(con, conference, CURRENT_SEASON)
    next_row = _write_standings_block(ws, conference, standings, start_row=1)

    # ensure_conference_slate_tab() clears cell CONTENT on a re-run
    # (delete_rows) but a sheet-level setting like freeze_panes isn't a
    # cell -- it survives untouched from a previous run unless explicitly
    # reset here, so an old run's freeze (or Excel's own manual "Freeze
    # Panes" if you ever toggled it by hand) can't linger and cause the
    # exact "can't scroll" symptom _write_slate_table()'s freeze=False is
    # meant to prevent going forward.
    ws.freeze_panes = None

    if season is None:
        ws.cell(row=next_row, column=1,
                value="No upcoming (incomplete) games found in the DB -- run the pull scripts first.").font = NOTE_FONT
        print(f"{conference} Slate: no upcoming week detected, {len(standings)} team(s) in standings.")
    else:
        conf_rows = [r for r in all_rows if r["home_conf"] == conference or r["away_conf"] == conference]
        if not conf_rows:
            ws.cell(row=next_row, column=1, value=f"No FBS games this week involve {conference}.").font = NOTE_FONT
            print(f"{conference} Slate: 0 games this week, {len(standings)} team(s) in standings.")
        else:
            _write_slate_table(ws, conf_rows, start_row=next_row, freeze=False)
            n_edges = sum(1 for r in conf_rows
                          if r["spread_edge"] is not None and abs(r["spread_edge"]) >= backtest.EDGE_THRESHOLD)
            print(f"{conference} Slate: {len(conf_rows)} game(s) ({n_edges} real edge), "
                  f"{len(standings)} team(s) in standings.")

    # Column A needs to fit both a team name (standings) and "Week" (slate) --
    # _write_slate_table() above just set every column's width for its own
    # needs, so re-widen A-F to whichever of the two blocks needs more,
    # rather than letting the slate table's narrower widths win.
    for col, width in zip("ABCDEF", [22, 11, 12, 12, 15, 15]):
        ws.column_dimensions[col].width = max(ws.column_dimensions[col].width or 0, width)


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
    ats_ws = ensure_mw_ats_tab(wb)
    update_mw_ats(ats_ws, con)
    sor_ws = ensure_sor_tab(wb)
    update_sor_tab(sor_ws, con)

    # Computed ONCE here and reused by both National Slate and every
    # per-conference tab below -- see _compute_national_slate_rows()'s own
    # docstring for why (Ridge/totals refitting is not free).
    season, week, all_rows = _compute_national_slate_rows(con)

    national_ws = ensure_national_slate_tab(wb)
    render_national_slate(national_ws, season, week, all_rows)

    for conference in sorted(FBS_CONFERENCES):
        conf_ws = ensure_conference_slate_tab(wb, conference)
        update_conference_slate(conf_ws, con, conference, season, week, all_rows)

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
