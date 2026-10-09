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
    a Model ATS Record block (the live model's own against-the-spread
    record: "Rolling ATS" is season-to-date, "Weekly ATS" is its own most
    recently graded week -- see _compute_ats_tracking()'s own docstring)
    on top, then every FBS game this week (not just Mountain West), Gamma's
    live spread, Ridge/Massey as informational candidates, the real market
    line, the edge between them (green-filled when it's a real,
    threshold-clearing lean -- same green/backtest.EDGE_THRESHOLD as MW
    Team ATS's Cover fill), and a straight-up Win/Loss grade once a game's
    final. Sorted by conference with AutoFilter on, so you can drop it down
    to one conference at a time. See _compute_national_slate_rows()'s own
    docstring for why XGBoost isn't on this one. Fully regenerated every run.
  - One "<Conference> Slate" tab per FBS conference (ACC Slate, Big Ten
    Slate, ... Mountain West Slate, 11 tabs total, in addition to keeping
    National Slate): that conference's own Model ATS Record block (same
    Rolling/Weekly shape as National Slate's, scoped to games involving this
    conference), then a standings block (each team's conference and overall
    win-loss record, sorted by conference record), then that conference's
    own slice of the exact same National Slate rows below it (a
    non-conference game involving two different conferences' teams shows
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
from teams import MW_TEAMS_2026, FBS_CONFERENCES, normalize_team_name, is_2026_mw_team  # noqa: E402

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


def update_weekly_slate(ws, predictions_path: Path, con, backfill_rows=None):
    import csv
    with open(predictions_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    # predict_week.py's CSV now covers the FULL national FBS slate (it used
    # to be narrowed to MW-involved games at the source -- see that script's
    # own comment on why that changed: other readers of this same CSV, like
    # export_site_data.py's Predictions page, need a live line for non-MW
    # games too). This tab is still MW-only by design though -- 41 rows,
    # would silently overflow/drop real MW games behind a flood of national
    # matchups on a busy week -- so the filtering that used to happen in
    # predict_week.py happens here instead, right before anything gets
    # written.
    total_rows = len(rows)
    rows = [r for r in rows if is_2026_mw_team(r["Home Team"]) or is_2026_mw_team(r["Away Team"])]
    if total_rows != len(rows):
        print(f"Weekly Slate: {total_rows} game(s) in predictions file, {len(rows)} involve a "
              f"2026 Mountain West team -- only those are written to this MW-only tab.")

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

    # Past weeks STAY on this tab (newest week on top, older below). Rows are
    # keyed by (week, home, away); a re-run updates the model lines in place
    # and never touches Market/Notes/Final Decision you typed (the market
    # columns are only overwritten when a CFBD line is actually posted).
    from copy import copy
    MAXR = 400
    existing = {}
    for r in range(2, max(ws.max_row, 43) + 1):
        home, away, wk = ws[f"D{r}"].value, ws[f"C{r}"].value, ws[f"A{r}"].value
        if home and away:
            existing[(wk, home, away)] = {c: ws[f"{c}{r}"].value for c in "ABCDEFIJOP"}

    written = 0
    for pred in rows:
        key = (int(pred["Week"]), pred["Home Team"], pred["Away Team"])
        rec = existing.setdefault(key, {c: None for c in "ABCDEFIJOP"})
        rec.update({"A": int(pred["Week"]), "B": pred["Date"], "C": pred["Away Team"], "D": pred["Home Team"],
                    "E": float(pred["Model Line (Home)"]), "I": float(pred["Model Total"])})
        written += 1
        game_id = pred.get("Game ID")
        if game_id is not None:
            try:
                spread, total = market.get(int(game_id), (None, None))
            except ValueError:
                spread, total = None, None
            if spread is not None:
                rec["F"] = round(spread, 1)
                filled_market += 1
            if total is not None:
                rec["J"] = round(total, 1)

    # Backfilled earlier-week MW games (only when that week isn't on the tab).
    weeks_present = {k[0] for k in existing}
    for b in (backfill_rows or []):
        if not (is_2026_mw_team(b["home_team"]) or is_2026_mw_team(b["away_team"])):
            continue
        if b["week"] in weeks_present:
            continue
        existing[(b["week"], b["home_team"], b["away_team"])] = {
            "A": b["week"], "B": b["date"], "C": b["away_team"], "D": b["home_team"],
            "E": b["gamma_spread"], "F": b["market_spread"], "I": b["model_total"], "J": b["market_total"],
            "O": None, "P": None}

    ordered = sorted(existing.values(), key=lambda d: (-(d["A"] or 0), str(d["B"] or ""), str(d["D"])))
    if len(ordered) > MAXR - 1:
        ordered = ordered[:MAXR - 1]
    last_template = max(ws.max_row, 2)
    for i, rec in enumerate(ordered):
        r = 2 + i
        if r > 42:   # beyond the prefilled rows: copy row 2's look
            for ci in range(1, 17):
                ws.cell(row=r, column=ci)._style = copy(ws.cell(row=2, column=ci)._style)
        for c in "ABCDEFIJOP":
            ws[f"{c}{r}"] = rec[c]
        ws[f"G{r}"] = f"=F{r}-E{r}"
        ws[f"H{r}"] = f'=IF(G{r}=0,"Pick\'em",IF(G{r}>0,"Home","Away"))'
        ws[f"K{r}"] = f"=I{r}-J{r}"
        ws[f"L{r}"] = f'=IF(K{r}=0,"Push",IF(K{r}>0,"Over","Under"))'
        ws[f"M{r}"] = (f'=IF(ABS(G{r})>=Settings!$B$5,H{r},'
                       f'IF(F{r}=0,"Pick\'em",IF(F{r}<0,"Home (auto)","Away (auto)")))')
        ws[f"N{r}"] = f'=IF(ABS(K{r})>=Settings!$B$6,L{r},IF(K{r}=0,"Push",L{r}&" (auto)"))'
    # Leftover rows below the written block (prefilled range only): blank the
    # inputs, keep the formulas.
    for r in range(2 + len(ordered), max(last_template, 42) + 1):
        for c in "ABCDEFIJOP":
            ws[f"{c}{r}"] = None

    # No frozen panes anywhere in this workbook (Cole's own call) -- this
    # tab is never recreated by this script (only build_tracker.py's
    # one-time setup makes it), so a workbook built before this change keeps
    # whatever build_tracker.py set unless it's explicitly cleared here on
    # every run.
    ws.freeze_panes = None

    print(f"Weekly Slate: wrote/updated {written} current-week matchup(s); {len(ordered)} row(s) total across weeks")
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


def _xgb_lookup():
    """{(week, home_team, away_team): Dub Beta (XGBoost) home spread} from the two places that hold it:
    model_comparison_results.csv (walk-forward line for every completed game) and the latest predictions
    CSV (current week, fresher -- overrides). Missing/unreadable files just mean fewer entries."""
    import csv
    out = {}
    cmp_path = CLEAN_DIR / "model_comparison_results.csv"
    try:
        with open(cmp_path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("season") == str(CURRENT_SEASON) and r.get("xgb_spread_home") not in (None, ""):
                    out[(int(r["week"]), r["home_team"], r["away_team"])] = round(float(r["xgb_spread_home"]), 1)
    except (OSError, ValueError, KeyError):
        pass
    pred = latest_predictions_file()
    if pred is not None:
        try:
            with open(pred, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("XGBoost Line (Home)") not in (None, ""):
                        out[(int(r["Week"]), r["Home Team"], r["Away Team"])] = round(float(r["XGBoost Line (Home)"]), 1)
        except (OSError, ValueError, KeyError):
            pass
    return out


def _spread_ats(model_line, market, away_pts, home_pts):
    """Against-the-spread result for ONE model's line: the model leans Home when Market - Model > 0 (same rule
    as backtest.grade_spread_pick), and the pick covers when (home - away) + market line says so. Win/Loss/Push,
    or None while the game is unplayed / no market line / no lean (model equals the market)."""
    if None in (model_line, market, away_pts, home_pts):
        return None
    edge = market - model_line
    # Within 1 point of the market the pick defaults to the Vegas favorite
    # (same rule as backtest.grade_spread_pick / FAVORITE_DEFAULT_EDGE).
    if abs(edge) < 1.0 and market != 0:
        lean_home = market < 0
    elif edge == 0:
        return None
    else:
        lean_home = edge > 0
    cover = (home_pts - away_pts) + market
    if cover == 0:
        return "Push"
    return "Win" if (cover > 0) == lean_home else "Loss"


def _apply_model_ats(rows):
    for r in rows:
        for key, line in (("gamma_ats", r.get("gamma_spread")), ("ridge_ats", r.get("ridge_spread")),
                          ("xgb_ats", r.get("xgb_spread")), ("massey_ats", r.get("massey_spread"))):
            r[key] = _spread_ats(line, r.get("market_spread"), r.get("away_pts"), r.get("home_pts"))


def _compute_national_slate_rows(con, season=None, week=None):
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
    # `season`/`week` default to the auto-detected upcoming week; passing both
    # explicitly is how main() backfills the most recent COMPLETED week so its
    # results stay on the tab (see _carry_forward_history()).
    if season is None or week is None:
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
    # Walk-forward cutoff: only games that started before this week's first
    # kickoff (same before_date backtest.py uses), so a week that is already
    # (partly) final is never fit on its own results.
    week_start = con.execute(
        "SELECT MIN(start_date) FROM games WHERE season = ? AND week = ?", [season, week]
    ).fetchone()[0]
    train_df = model.load_training_frame(con, before_date=week_start)
    # Ratings as they stood ENTERING this week (through_week=week - 1), not
    # replay_ratings(con)'s "every completed game to date." Once some of this
    # week's games are final but the week is still "current" (the usual
    # Sat-night/Sun state), the no-cutoff version already contains those
    # games' own results, so the line shown for them was hindsight -- and
    # disagreed with the walk-forward line the backtest/ATS record grades
    # (gamma_model.ratings_entering_week()). For a genuinely upcoming week
    # nothing in `week` is complete yet, so this is identical to before.
    gamma_ratings, gamma_warned = gamma_model.replay_ratings(con, season=season, through_week=week - 1)
    if gamma_warned:
        print(f"  [National Slate] Dub Gamma: {len(gamma_warned)} team(s) had no Moore seed rating, "
              f"defaulted to {gamma_model.DEFAULT_SEED_RATING:.2f}: {gamma_warned}")
    massey_ratings = massey_model.ratings_entering_week(con, season, week)

    ridge_spread_home = {}
    if len(train_df) >= 10:
        pipe, _ = model.fit_margin_model(train_df)
        ridge_margin = model.predict_margin(pipe, upcoming)
        ridge_spread_home = dict(zip(upcoming["game_id"].astype(int), -ridge_margin))

    totals_train = totals_model.load_totals_training_frame(con, before_date=week_start)
    total_pipe, _ = totals_model.fit_total_model(totals_train)
    wk_totals_features = totals_model.load_upcoming_totals_frame(con, season, week)
    total_map = {}
    if not wk_totals_features.empty:
        total_preds = totals_model.predict_total(total_pipe, wk_totals_features)
        total_map = dict(zip(wk_totals_features["game_id"].astype(int), total_preds))

    xgb_map = _xgb_lookup()
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
            "xgb_spread": xgb_map.get((int(row.week), row.home_team, row.away_team)),
            "model_total": round(model_total, 1) if model_total is not None else None,
            "market_total": round(market_total, 1) if market_total is not None else None,
            "total_edge": round(total_edge, 1) if total_edge is not None else None,
            "away_pts": away_pts if completed else None,
            "home_pts": home_pts if completed else None,
            "model_result": model_result,
        })

    rows.sort(key=lambda r: (r["home_conf"], r["date"], r["home_team"]))
    return season, week, rows


def _compute_ats_tracking(con, season, current_week=None):
    """
    The live model's (Dub Gamma's) own against-the-spread record, scoped two
    ways for National Slate (all FBS) and each "<Conference> Slate" tab:
    "rolling" = every graded game this season to date, "weekly" = just that
    scope's own most recently graded week (not necessarily the upcoming
    week _compute_national_slate_rows() found -- a conference on a bye, or
    a week still in progress with nothing final yet, still gets a
    meaningful "last week" number this way instead of an empty one).

    Reuses backtest.run_backtest()'s walk-forward grading -- the EXACT same
    game-by-game ATS result (bet_result: Win/Loss/Push, from
    backtest.grade_spread_pick()) the website's Tracking page/live model
    record is built from -- rather than re-deriving a second copy of "did
    the model's pick cover" logic here. Computed ONCE in main() and reused
    by every tab, same "expensive, pay for it once" discipline
    _compute_national_slate_rows() already follows for its own Ridge/totals
    refitting -- run_backtest() replays a walk-forward fit per test week
    across the whole DB, so calling it again per conference (12x) would
    meaningfully slow down an already multi-minute pipeline run.

    A push counts in the W-L-P record shown but, same convention as MW Team
    ATS's own Cover %, is excluded from the percentage itself (graded = wins
    + losses). A pick'em with no bet_result at all (no favorite to grade
    against) is skipped entirely -- there's no side to have covered. Rows
    outside `season` (e.g. synthetic/prior-season training data
    run_backtest() also grades) are excluded, same "current season only"
    scope every other Excel ATS tab uses.

    Returns {"National": {"rolling": stat, "weekly": stat}, "<conference>":
    {...}, ...} where `stat` is {"w", "l", "p", "pct", "week"} -- `week` is
    the week number "weekly" actually reflects (None if nothing's graded in
    that scope yet this season), `pct` is None whenever graded == 0.
    """
    bt = backtest.run_backtest(con)
    bt = bt[bt["season"] == season]

    conf_by_game = {
        gid: (hc, ac) for gid, hc, ac in con.execute(
            "SELECT game_id, home_conference, away_conference FROM games WHERE season = ?", [season]
        ).fetchall()
    }

    graded_rows = []
    for row in bt.itertuples():
        if row.bet_result is None:
            continue
        gid = int(row.game_id)
        hc, ac = conf_by_game.get(gid, (None, None))
        graded_rows.append({"bet_result": row.bet_result, "week": int(row.week), "home_conf": hc, "away_conf": ac})

    def _stat(rows, week=None):
        w = sum(1 for r in rows if r["bet_result"] == "Win")
        l = sum(1 for r in rows if r["bet_result"] == "Loss")
        p = sum(1 for r in rows if r["bet_result"] == "Push")
        graded = w + l
        return {"w": w, "l": l, "p": p, "pct": (round(w / graded, 3) if graded else None), "week": week}

    def _scope_tracking(rows):
        rolling = _stat(rows)
        weeks = {r["week"] for r in rows}
        if not weeks:
            return {"rolling": rolling, "weekly": _stat([], week=None)}
        # "Weekly" = the most recent week BEFORE the current/upcoming one (a week that is still being
        # played -- e.g. its Wed/Thu games are final but the Saturday slate isn't -- is not "last week").
        done = [w for w in weeks if current_week is None or w < current_week]
        latest_week = max(done) if done else max(weeks)
        weekly = _stat([r for r in rows if r["week"] == latest_week], week=latest_week)
        return {"rolling": rolling, "weekly": weekly}

    out = {"National": _scope_tracking(
        [r for r in graded_rows if r["home_conf"] in FBS_CONFERENCES or r["away_conf"] in FBS_CONFERENCES]
    )}
    for conference in FBS_CONFERENCES:
        out[conference] = _scope_tracking(
            [r for r in graded_rows if r["home_conf"] == conference or r["away_conf"] == conference]
        )
    return out


_SLATE_HEADERS = [
    "Week", "Date", "Home Conf", "Away Conf", "Away Team", "Home Team",
    "Model Line (Home)", "Market Line (Home)", "Spread Edge (pts)",
    "Ridge Line (Home)", "Massey Line (Home)",
    "Model Total", "Market Total", "Total Edge (pts)",
    "Away Pts", "Home Pts", "Model Result (SU)",
    "XGBoost Line (Home)", "Gamma ATS", "Ridge ATS", "XGBoost ATS", "Massey ATS",
]
_SLATE_COL_WIDTHS = [6, 11, 12, 12, 15, 15, 16, 16, 14, 15, 15, 11, 11, 12, 9, 9, 12, 15, 11, 11, 12, 12]


def _write_slate_table(ws, rows, start_row=1, freeze=True, autofilter=True):
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
            r.get("xgb_spread"), r.get("gamma_ats"), r.get("ridge_ats"), r.get("xgb_ats"), r.get("massey_ats"),
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
        for col_idx, key in ((19, "gamma_ats"), (20, "ridge_ats"), (21, "xgb_ats"), (22, "massey_ats")):
            v = r.get(key)
            cell = ws.cell(row=i, column=col_idx)
            cell.alignment = Alignment(horizontal="center")
            if v == "Win":
                cell.fill = CORRECT_FILL
            elif v == "Loss":
                cell.fill = INCORRECT_FILL
            elif v == "Push":
                cell.fill = PUSH_FILL

    last_row = header_row + len(rows)
    # Stacked multi-week tabs can't carry one filter range across several
    # separate tables, so they pass autofilter=False.
    if autofilter:
        ws.auto_filter.ref = f"A{header_row}:V{last_row}"
    for col, width in zip("ABCDEFGHIJKLMNOPQRSTUV", _SLATE_COL_WIDTHS):
        ws.column_dimensions[col].width = width
    # Always cleared, never set -- see this function's own docstring.
    # Explicit (not just "never assigned") so a workbook that already has a
    # frozen pane from before this change gets it cleared on the next run,
    # the same "reset stale state" fix update_conference_slate() already
    # does for freeze_panes elsewhere.
    ws.freeze_panes = None
    return last_row


ATS_SUMMARY_HEADER_FILL = PatternFill("solid", fgColor="7B3F00")  # dark brown -- distinct from both the slate's navy and standings' green headers
ATS_SUMMARY_HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)


def _add_su_tracking(ats_tracking, rows, season_rows_week=None):
    """Adds "su_weekly" to every scope's tracking dict: the Model Result column's own record
    (did Dub Gamma's favorite win the game straight up) for the SAME week that scope's weekly
    ATS shows. Different from ATS (the market line decides ATS) -- both are shown side by side."""
    def su(scope_rows, wk):
        w = sum(1 for r in scope_rows if r["week"] == wk and r["model_result"] == "Win")
        l = sum(1 for r in scope_rows if r["week"] == wk and r["model_result"] == "Loss")
        return {"w": w, "l": l, "pct": (round(w / (w + l), 3) if (w + l) else None), "week": wk}
    scopes = {"National": rows}
    for conf in FBS_CONFERENCES:
        scopes[conf] = [r for r in rows if r["home_conf"] == conf or r["away_conf"] == conf]
    for name, sr in scopes.items():
        t = ats_tracking.get(name)
        if t and t["weekly"].get("week"):
            t["su_weekly"] = su(sr, t["weekly"]["week"])


def _write_ats_summary_block(ws, label, tracking, start_row=1):
    """
    Small 2-row stat block: the live model's (Dub Gamma's) own
    against-the-spread record for this scope (`label` -- "National" or a
    conference name), season-to-date ("Rolling ATS") and for its own most
    recently graded week ("Weekly ATS") -- see _compute_ats_tracking()'s own
    docstring for exactly what counts as graded and how the percentage is
    computed. Returns the row number the caller should continue writing at
    (one blank row below this block).

    `tracking` may be None/empty (e.g. _compute_ats_tracking() found nothing
    graded yet this season in this scope) -- the block still renders with
    blank W-L-P/no week rather than being skipped, so every tab's layout
    starts at the same row regardless of how much history exists yet.
    """
    ws.cell(row=start_row, column=1, value=f"{label} Model ATS Record").font = SUBTITLE_FONT
    header_row = start_row + 1
    headers = ["Metric", "Record (W-L-P)", "ATS %"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row, column=c, value=h)
        cell.font = ATS_SUMMARY_HEADER_FONT
        cell.fill = ATS_SUMMARY_HEADER_FILL
        cell.alignment = Alignment(horizontal="center")

    empty_stat = {"w": 0, "l": 0, "p": 0, "pct": None, "week": None}
    rolling = (tracking or {}).get("rolling") or empty_stat
    weekly = (tracking or {}).get("weekly") or empty_stat
    weekly_label = f"Weekly ATS (Wk {weekly['week']})" if weekly.get("week") else "Weekly ATS (no graded games yet)"

    r = header_row + 1
    su = (tracking or {}).get("su_weekly")
    metric_rows = [("Rolling ATS (Season)", rolling), (weekly_label, weekly)]
    if su:
        metric_rows.append((f"Model pick W-L, straight-up (Wk {su['week']})", {**su, "p": None}))
    for metric_label, stat in metric_rows:
        ws.cell(row=r, column=1, value=metric_label).font = FORMULA_FONT
        rec = f"{stat['w']}-{stat['l']}" + ("" if stat.get("p") is None else f"-{stat['p']}")
        ws.cell(row=r, column=2, value=rec).font = FORMULA_FONT
        pct_cell = ws.cell(row=r, column=3, value=stat["pct"])
        pct_cell.font = FORMULA_FONT
        if stat["pct"] is not None:
            pct_cell.number_format = "0.0%"
        r += 1
    return r + 1  # one blank row, then the caller's own block starts here



def _read_slate_history(ws):
    """Parses every stacked week block already on the National Slate sheet
    (header row = "Week"/"Date" in A/B) back into row dicts, BEFORE the sheet
    is wiped, so earlier weeks' lines survive each regeneration."""
    out = []
    in_table = False
    for row in ws.iter_rows(values_only=True):
        a = row[0] if row else None
        if a == "Week" and len(row) > 1 and row[1] == "Date":
            in_table = True
            continue
        if not in_table:
            continue
        if not isinstance(a, (int, float)):
            in_table = False
            continue
        v = list(row[:22]) + [None] * (22 - len(row[:22]))
        out.append({
            "week": int(v[0]), "date": str(v[1]) if v[1] is not None else "",
            "home_conf": v[2] or "", "away_conf": v[3] or "",
            "away_team": v[4], "home_team": v[5],
            "gamma_spread": v[6], "market_spread": v[7], "spread_edge": v[8],
            "ridge_spread": v[9], "massey_spread": v[10],
            "model_total": v[11], "market_total": v[12], "total_edge": v[13],
            "away_pts": v[14], "home_pts": v[15], "model_result": v[16],
            "xgb_spread": v[17],
        })
    return out


def _carry_forward_history(con, season, week, current_rows, history):
    """
    Current-week rows + every earlier week's rows, so past results are never
    erased: carried-forward rows get their final score/Model Result refreshed
    from `games` (a game that was unplayed last run may be final now), and the
    most recent completed week is backfilled from the DB (walk-forward lines)
    if the sheet doesn't hold it yet -- that's how week 5 comes back the first
    time this runs. Returns a flat list; each row carries its own "week".
    """
    if season is None:
        return current_rows
    history = [h for h in history if h["week"] < week]          # a higher week = stale prior season
    have_weeks = {h["week"] for h in history}
    backfill_week = week - 1
    if backfill_week >= 1 and backfill_week not in have_weeks:
        print(f"  Backfilling week {backfill_week} (not on the sheet yet)...")
        _, _, back_rows = _compute_national_slate_rows(con, season, backfill_week)
        history.extend(back_rows)

    results = {}
    neutral = {}
    for home, away, wk, completed, hp, ap, neu in con.execute(
        "SELECT home_team, away_team, week, completed, home_points, away_points, neutral_site "
        "FROM games WHERE season = ?",
        [season],
    ).fetchall():
        results[(wk, home, away)] = (completed, hp, ap)
        neutral[(wk, home, away)] = bool(neu)

    # Re-derive each carried-forward row's Dub Gamma line (and the edge/result that follow from it)
    # from walk-forward ratings every run, so a model change (e.g. a new HFA) can't leave old weeks
    # showing stale lines that disagree with the ATS box (which is regraded every run).
    ratings_by_week = {}
    for h in history:
        wk = h["week"]
        if wk not in ratings_by_week:
            ratings_by_week[wk] = gamma_model.replay_ratings(con, season=season, through_week=max(wk - 1, 1))[0]
        key = (wk, h["home_team"], h["away_team"])
        if key in neutral:
            gs = gamma_model.predict_spread_home(ratings_by_week[wk], h["home_team"], h["away_team"],
                                                 neutral_site=neutral[key])
            h["gamma_spread"] = round(gs, 1)
            if h.get("market_spread") is not None:
                h["spread_edge"] = round(h["market_spread"] - gs, 1)
    xgb_map = _xgb_lookup()
    massey_by_week = {}
    for h in history:
        wk = h["week"]
        key = (wk, h["home_team"], h["away_team"])
        if wk not in massey_by_week:
            massey_by_week[wk] = massey_model.ratings_entering_week(con, season, wk)
        if massey_by_week[wk] and key in neutral:
            h["massey_spread"] = round(massey_model.predict_spread_home(
                massey_by_week[wk], h["home_team"], h["away_team"], neutral_site=neutral[key]), 1)
        if key in xgb_map:
            h["xgb_spread"] = xgb_map[key]      # else keep whatever the sheet already had
    for h in history:
        completed, hp, ap = results.get((h["week"], h["home_team"], h["away_team"]), (False, None, None))
        if completed and hp is not None and ap is not None:
            h["home_pts"], h["away_pts"] = hp, ap
            margin = hp - ap
            gm = -h["gamma_spread"] if h["gamma_spread"] is not None else 0
            h["model_result"] = (None if (gm == 0 or margin == 0)
                                 else ("Win" if (margin > 0) == (gm > 0) else "Loss"))
    return list(current_rows) + history


def _write_stacked_slates(ws, rows, start_row):
    """One block per week -- 'Week N' subtitle, header, rows, blank line --
    newest week on top, older weeks below. Returns rows written (all weeks)."""
    by_week = {}
    for r in rows:
        by_week.setdefault(r["week"], []).append(r)
    r0 = start_row
    ws.auto_filter.ref = None
    for wk in sorted(by_week, reverse=True):
        ws.cell(row=r0, column=1, value=f"Week {wk}").font = SUBTITLE_FONT
        last = _write_slate_table(ws, by_week[wk], start_row=r0 + 1, freeze=False, autofilter=False)
        r0 = last + 2
    return len(rows)


def render_national_slate(ws, season, week, rows, ats_tracking):
    """Thin writer -- all the computing already happened in
    _compute_national_slate_rows()/_compute_ats_tracking(), this just
    renders it onto the tab. The ATS summary block renders first and always
    -- it doesn't depend on there being an upcoming week to show a slate
    for, so a bye week (season/rows still show their own placeholder note)
    doesn't hide the season's ATS record."""
    next_row = _write_ats_summary_block(ws, "National", ats_tracking, start_row=1)

    if season is None:
        ws.cell(row=next_row, column=1,
                value="No upcoming (incomplete) games found in the DB -- run the pull scripts first.").font = NOTE_FONT
        print("National Slate: no upcoming week detected -- left a placeholder note.")
        return
    if not rows:
        ws.cell(row=next_row, column=1, value=f"No FBS games found for season {season}, week {week}.").font = NOTE_FONT
        print(f"National Slate: no FBS games for season {season} week {week}.")
        return

    _write_stacked_slates(ws, rows, next_row)
    cur = [r for r in rows if r["week"] == week]
    n_edges = sum(1 for r in cur if r["spread_edge"] is not None and abs(r["spread_edge"]) >= backtest.EDGE_THRESHOLD)
    print(f"National Slate: {len(rows)} FBS game(s) across {len({r['week'] for r in rows})} week(s); "
          f"week {week}: {len(cur)} game(s), {n_edges} with a real spread edge >= {backtest.EDGE_THRESHOLD} pts")


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


def update_conference_slate(ws, con, conference, season, week, all_rows, ats_tracking):
    """
    Model ATS summary block, then standings block (see
    _conference_standings()), then this conference's own slice of the
    already-computed National Slate rows below it -- a non-conference game
    (e.g. a Mountain West team @ a Big Ten team) shows up on BOTH
    conferences' tabs, same as it would on either conference's real
    schedule. Reuses `all_rows` as computed once in main() rather than
    recomputing anything per conference, so a game here can never show
    different numbers than the same game on National Slate. `ats_tracking`
    is this one conference's own entry from _compute_ats_tracking()'s
    return dict (already computed once in main() too).
    """
    ats_next_row = _write_ats_summary_block(ws, conference, ats_tracking, start_row=1)

    # Standings reflect completed games for CURRENT_SEASON regardless of
    # whether an upcoming week was detected -- a None `season` here would
    # just mean "no upcoming week to build a slate for," not "no season."
    standings = _conference_standings(con, conference, CURRENT_SEASON)
    next_row = _write_standings_block(ws, conference, standings, start_row=ats_next_row)

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
            _write_stacked_slates(ws, conf_rows, next_row)
            n_edges = sum(1 for r in conf_rows if r["week"] == week
                          and r["spread_edge"] is not None and abs(r["spread_edge"]) >= backtest.EDGE_THRESHOLD)
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

    # Earlier weeks stay on the tabs: read what's already on National Slate
    # BEFORE it gets wiped below, carry it forward (results refreshed), and
    # backfill the latest completed week if it isn't there yet.
    history = _read_slate_history(wb[NATIONAL_SLATE_SHEET]) if NATIONAL_SLATE_SHEET in wb.sheetnames else []
    all_rows = _carry_forward_history(con, season, week, all_rows, history)
    _apply_model_ats(all_rows)
    update_weekly_slate(wb["Weekly Slate"], pred_file, con, backfill_rows=all_rows)

    # Also computed ONCE here and reused the same way -- see
    # _compute_ats_tracking()'s own docstring for why (it replays
    # backtest.run_backtest()'s full walk-forward grading, which is not
    # cheap either).
    print("Computing model ATS tracking (this replays the full walk-forward backtest, may take a while)...")
    ats_tracking = _compute_ats_tracking(con, CURRENT_SEASON, current_week=week)
    _add_su_tracking(ats_tracking, all_rows)

    national_ws = ensure_national_slate_tab(wb)
    render_national_slate(national_ws, season, week, all_rows, ats_tracking.get("National"))

    for conference in sorted(FBS_CONFERENCES):
        conf_ws = ensure_conference_slate_tab(wb, conference)
        update_conference_slate(conf_ws, con, conference, season, week, all_rows, ats_tracking.get(conference))

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
