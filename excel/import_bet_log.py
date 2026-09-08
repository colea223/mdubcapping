"""
Pulls new bet submissions from the site's Log Bet page (docs/log-bet.html)
and appends them into the Bet Log tab of MW_Handicapping_Tracker.xlsx.

WHY THIS EXISTS: docs/ is a plain static GitHub Pages site (no server -- see
README's "The website" section), so a browser form on it can never write
directly to a file on your machine. log-bet.html instead POSTs each
submission to a small Google Apps Script Web App bound to a Google Sheet you
own (see google_apps_script/bet_log_webapp.gs for the script + one-time
setup) -- that Sheet is the durable landing spot. This script is the other
half: it reads whatever's landed there via the Apps Script's doGet (gated by
a shared secret token that's never exposed on the public site -- see
.env.example / config.BET_WEBAPP_TOKEN) and appends each new row into the Bet
Log tab's columns A-H, exactly what you'd type in by hand:
    Date, Week, Matchup, Bet Type, Side, Line Taken, Odds (American), Stake (units)
Columns I (Closing Line) and K (Result) are deliberately left blank -- those
are only known later (once the market closes / the game is graded), same as
a hand-entered bet. Columns J/L/M are pre-built FORMULAS (CLV, Units
Won/Lost, Running Bankroll) already sitting on every template row and are
never touched here.

DEDUPING: each submission gets a unique id (a UUID minted by the Apps Script
on doPost) -- this script keeps a local record of which ids it's already
imported (excel/.bet_log_imported_ids.json, gitignored) so re-running it
never double-enters the same bet. Nothing is ever deleted from the Google
Sheet by this script -- it stays a full history/backup of every submission,
independent of the tracker.

Usage:
    source .venv/bin/activate     (or the Windows equivalent)
    python excel/import_bet_log.py
"""
import json
import sys
import time
from pathlib import Path

import openpyxl
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from config import BET_WEBAPP_URL, BET_WEBAPP_TOKEN  # noqa: E402

TRACKER_PATH = Path(__file__).resolve().parent / "MW_Handicapping_Tracker.xlsx"
IMPORTED_IDS_PATH = Path(__file__).resolve().parent / ".bet_log_imported_ids.json"
BET_LOG_ROWS = range(2, 43)  # matches build_tracker.py's Bet Log layout (row 1 = headers)


def _load_imported_ids():
    if not IMPORTED_IDS_PATH.exists():
        return set()
    return set(json.loads(IMPORTED_IDS_PATH.read_text()))


def _save_imported_ids(ids):
    IMPORTED_IDS_PATH.write_text(json.dumps(sorted(ids), indent=2))


def fetch_submissions():
    resp = requests.get(BET_WEBAPP_URL, params={"token": BET_WEBAPP_TOKEN}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Web app returned an error: {payload.get('error')}")
    return payload.get("rows", [])


def first_empty_row(ws):
    for r in BET_LOG_ROWS:
        if ws[f"A{r}"].value in (None, ""):
            return r
    return None  # sheet is full -- see main()'s warning


def write_row(ws, row_num, sub):
    ws[f"A{row_num}"] = sub["date"]
    ws[f"B{row_num}"] = int(sub["week"])
    ws[f"C{row_num}"] = sub["matchup"]
    ws[f"D{row_num}"] = sub["bet_type"]
    ws[f"E{row_num}"] = sub["side"]
    ws[f"F{row_num}"] = float(sub["line"])
    ws[f"G{row_num}"] = int(sub["odds"])
    ws[f"H{row_num}"] = float(sub["stake"])
    # I (Closing Line) and K (Result) intentionally left blank. J/L/M are
    # pre-built formulas already sitting on this template row -- never
    # written here.


def main():
    if not BET_WEBAPP_URL or not BET_WEBAPP_TOKEN:
        print("BET_WEBAPP_URL / BET_WEBAPP_TOKEN not set in .env -- see .env.example and "
              "google_apps_script/bet_log_webapp.gs for setup.")
        return
    if not TRACKER_PATH.exists():
        print(f"Tracker workbook not found at {TRACKER_PATH}.")
        return

    print("Fetching submissions from the Log Bet web app...")
    submissions = fetch_submissions()
    print(f"  {len(submissions)} total submission(s) on the sheet")

    imported_ids = _load_imported_ids()
    new_subs = [s for s in submissions if s["id"] not in imported_ids]
    if not new_subs:
        print("Nothing new to import.")
        return
    new_subs.sort(key=lambda s: s.get("submitted_at", ""))  # oldest first

    wb = openpyxl.load_workbook(TRACKER_PATH)  # formulas preserved as formulas, not evaluated
    ws = wb["Bet Log"]

    written = 0
    for sub in new_subs:
        row_num = first_empty_row(ws)
        if row_num is None:
            print(f"  Bet Log is full (rows {BET_LOG_ROWS.start}-{BET_LOG_ROWS.stop - 1}) -- "
                  f"stopping with {len(new_subs) - written} submission(s) still unimported. "
                  "Add more template rows (copy row 42's J/L/M formulas down) and re-run.")
            break
        write_row(ws, row_num, sub)
        imported_ids.add(sub["id"])
        print(f"  row {row_num}: {sub['side']} ({sub['bet_type']}), {sub['matchup']}")
        written += 1

    if written:
        wb.save(TRACKER_PATH)
        _save_imported_ids(imported_ids)
        print(f"\nImported {written} new bet(s) into Bet Log. Saved {TRACKER_PATH}. "
              "Open it in Excel -- formulas recalculate automatically on open.")
    else:
        print("Nothing written.")


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
