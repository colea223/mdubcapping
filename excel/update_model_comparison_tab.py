"""
Adds/refreshes a "Model Comparison" tab in MW_Handicapping_Tracker.xlsx --
Ridge (the live model) vs. XGBoost (a candidate), graded against Vegas's
closing spread, side by side. Reads src/model_comparison.py's output
(data/clean/model_comparison_results.csv -- run that script first).

NON-DESTRUCTIVE, same rule as excel/update_tracker.py: this only ever
touches the "Model Comparison" sheet -- creating it if it doesn't exist yet,
or clearing and rewriting ONLY that sheet's cells if it does. Every other
tab (Read Me, Settings, Weekly Slate, Bet Log, Team Profiles, and anything
else you've added) is left completely untouched.

This does NOT change what Weekly Slate uses, and does NOT touch Ridge's
role as the live model anywhere in this project -- it's an informational
side-by-side, nothing more, per the explicit instruction that XGBoost isn't
taking over yet.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python src/model_comparison.py          # produces the CSV this reads
    python excel/update_model_comparison_tab.py
"""
import sys
from pathlib import Path

import openpyxl
import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import CLEAN_DIR  # noqa: E402

TRACKER_PATH = Path(__file__).resolve().parent / "MW_Handicapping_Tracker.xlsx"
RESULTS_PATH = CLEAN_DIR / "model_comparison_results.csv"
SHEET_NAME = "Model Comparison"

# Same visual language as build_tracker.py's other tabs, redefined here
# rather than imported -- build_tracker.py runs (and saves a fresh workbook)
# at import time, so importing it would clobber the real tracker.
FONT_NAME = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=FONT_NAME, bold=True, color="FFFFFF", size=10)
FORMULA_FONT = Font(name=FONT_NAME, color="000000", size=10)
TITLE_FONT = Font(name=FONT_NAME, bold=True, size=14)
SECTION_FONT = Font(name=FONT_NAME, bold=True, size=11, color="1F3864")
NOTE_FONT = Font(name=FONT_NAME, italic=True, size=9, color="666666")
WIN_FILL = PatternFill("solid", fgColor="E2EFDA")
LOSS_FILL = PatternFill("solid", fgColor="FCE4EC")
THIN = Side(style="thin", color="CCCCCC")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def style_header_row(ws, row, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = BORDER


def _summary_row(ws, row, name, s):
    n_bets = s.get("n_bets") or 0
    ws.cell(row=row, column=1, value=name).font = FORMULA_FONT
    ws.cell(row=row, column=2, value=s.get("n_games", 0)).font = FORMULA_FONT
    ws.cell(row=row, column=3, value=n_bets).font = FORMULA_FONT
    ws.cell(row=row, column=4, value=s.get("wins")).font = FORMULA_FONT
    ws.cell(row=row, column=5, value=s.get("losses")).font = FORMULA_FONT
    ws.cell(row=row, column=6, value=s.get("pushes")).font = FORMULA_FONT
    win_pct = s.get("ats_win_rate")
    ws.cell(row=row, column=7, value=(f"{win_pct:.1%}" if win_pct is not None else "n/a")).font = FORMULA_FONT
    roi = s.get("roi_flat_stake")
    ws.cell(row=row, column=8, value=(f"{roi:+.1%}" if roi is not None else "n/a")).font = FORMULA_FONT


def write_summary_block(ws, df, start_row, title, season_filter=None):
    """One 'Overall / MW-involved x Ridge / XGBoost' block. Returns the next free row."""
    from model_comparison import summarize

    sl = df if season_filter is None else df[df["season"] == season_filter]
    r = start_row
    ws.cell(row=r, column=1, value=title).font = SECTION_FONT
    r += 1
    headers = ["Model", "Games Graded", "Bets Made", "Wins", "Losses", "Pushes", "ATS Win %", "ROI (flat stake)"]
    for c, h in enumerate(headers, start=1):
        ws.cell(row=r, column=c, value=h)
    style_header_row(ws, r, len(headers))
    r += 1

    for prefix, label in [("ridge", "Ridge (live model)"), ("xgb", "XGBoost (candidate)")]:
        s_overall = summarize(sl, prefix, "overall")
        _summary_row(ws, r, f"{label} -- all FBS", s_overall)
        r += 1
        s_mw = summarize(sl[sl["is_mw_game"]], prefix, "mw")
        _summary_row(ws, r, f"{label} -- MW-involved", s_mw)
        r += 1
    return r + 1


def write_detail_table(ws, df, start_row, season):
    """Per-game detail for the CURRENT SEASON's Mountain West games only --
    same scoping convention as Weekly Slate/Bet Log (this season, MW focus),
    keeping the sheet a quick weekly read rather than an 11-season dump. The
    full history stays in data/clean/model_comparison_results.csv."""
    mw_this_season = df[(df["season"] == season) & (df["is_mw_game"])].sort_values(
        ["week", "home_team"]
    )

    r = start_row
    ws.cell(row=r, column=1, value=f"{season} Mountain West Games -- Game by Game").font = SECTION_FONT
    r += 1
    headers = [
        "Week", "Matchup", "Final Score", "Vegas Spread (Home)", "Actual Margin (Home)",
        "Ridge Line", "Ridge Edge", "Ridge Lean", "Ridge Result",
        "XGBoost Line", "XGBoost Edge", "XGBoost Lean", "XGBoost Result",
        "Models Agree?",
    ]
    for c, h in enumerate(headers, start=1):
        ws.cell(row=r, column=c, value=h)
    style_header_row(ws, r, len(headers))
    header_row = r
    r += 1

    if mw_this_season.empty:
        ws.cell(row=r, column=1, value="No graded Mountain West games yet this season.").font = NOTE_FONT
        return r + 1, header_row

    def fmt_spread(v):
        if v is None or pd.isna(v):
            return "--"
        return f"+{v:.1f}" if v > 0 else f"{v:.1f}"

    def result_text(result, lean):
        # A CSV round-trip turns a missing (never-bet) result into NaN, not
        # Python None -- and `nan or fallback` is a trap (NaN is truthy in
        # Python), so this checks pd.isna() explicitly rather than relying
        # on plain truthiness.
        if isinstance(result, str) and result and not pd.isna(result):
            return result
        return "Lean only" if lean != "Pick'em" else "--"

    def fmt_score(row):
        # Same Away @ Home order as the Matchup column, so the two read
        # together naturally (e.g. "Wyoming @ Boise State" / "20-34").
        if pd.isna(row.away_points) or pd.isna(row.home_points):
            return "--"
        return f"{int(row.away_points)}-{int(row.home_points)}"

    for row in mw_this_season.itertuples():
        matchup = f"{row.away_team} @ {row.home_team}"
        ridge_result_text = result_text(row.ridge_result, row.ridge_lean)
        xgb_result_text = result_text(row.xgb_result, row.xgb_lean)
        values = [
            int(row.week), matchup, fmt_score(row),
            fmt_spread(row.market_spread_home), fmt_spread(row.actual_margin),
            fmt_spread(row.ridge_spread_home), fmt_spread(row.ridge_edge),
            row.ridge_lean, ridge_result_text,
            fmt_spread(row.xgb_spread_home), fmt_spread(row.xgb_edge),
            row.xgb_lean, xgb_result_text,
            ("Yes" if row.models_agree is True else ("No" if row.models_agree is False else "n/a")),
        ]
        for c, v in enumerate(values, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = FORMULA_FONT
            if c == 9 and row.ridge_result in ("Win", "Loss"):
                cell.fill = WIN_FILL if row.ridge_result == "Win" else LOSS_FILL
            if c == 13 and row.xgb_result in ("Win", "Loss"):
                cell.fill = WIN_FILL if row.xgb_result == "Win" else LOSS_FILL
        r += 1

    return r + 1, header_row


def build_sheet(wb, df):
    if SHEET_NAME in wb.sheetnames:
        del wb[SHEET_NAME]
    ws = wb.create_sheet(SHEET_NAME)
    ws.sheet_view.showGridLines = False

    ws["A1"] = "Ridge vs. XGBoost -- Model Comparison"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = (
        "Informational only. Ridge is still the live model everywhere else in this workbook and on "
        "the website -- XGBoost is a candidate being evaluated here, not a replacement."
    )
    ws["A2"].font = NOTE_FONT

    current_season = int(df["season"].max())

    row = 4
    row = write_summary_block(ws, df, row, "All-Time (every season in the backtest)")
    row = write_summary_block(ws, df, row, f"{current_season} Season Only", season_filter=current_season)

    agree_rate = df["models_agree"].dropna().mean() if df["models_agree"].notna().any() else None
    if agree_rate is not None:
        ws.cell(row=row, column=1,
                value=f"Ridge and XGBoost agree on which side to lean in {agree_rate:.1%} of all graded games.").font = NOTE_FONT
        row += 2

    row, header_row = write_detail_table(ws, df, row, current_season)

    autosize(ws, [8, 26, 12, 18, 18, 12, 12, 11, 12, 12, 12, 11, 12, 13])
    ws.freeze_panes = f"A{header_row + 1}"


def autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def main():
    if not RESULTS_PATH.exists():
        print(f"{RESULTS_PATH} not found. Run 'python src/model_comparison.py' first.")
        return
    if not TRACKER_PATH.exists():
        print(f"Tracker workbook not found at {TRACKER_PATH}. Run excel/build_tracker.py first.")
        return

    df = pd.read_csv(RESULTS_PATH)
    if df.empty:
        print("model_comparison_results.csv is empty -- nothing to write yet.")
        return
    # models_agree round-trips through CSV as True/False/NaN -- pandas already
    # reads that correctly as object dtype with real bool/NaN values.

    wb = openpyxl.load_workbook(TRACKER_PATH)
    build_sheet(wb, df)
    wb.save(TRACKER_PATH)
    print(f"'{SHEET_NAME}' tab written to {TRACKER_PATH}. Every other tab is untouched.")


if __name__ == "__main__":
    main()
