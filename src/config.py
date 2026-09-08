"""
Central config for the Mountain West handicapping pipeline.
Loads secrets from .env (copy .env.example -> .env and fill in your CFBD key).
"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

CFBD_API_KEY = os.getenv("CFBD_API_KEY", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# Only needed for excel/import_bet_log.py -- the read side of the Log Bet
# page's Google Apps Script Web App (see google_apps_script/bet_log_webapp.gs
# for setup). BET_WEBAPP_URL is the deployed Web App's /exec URL;
# BET_WEBAPP_TOKEN must match the READ_TOKEN you set inside that script.
BET_WEBAPP_URL = os.getenv("BET_WEBAPP_URL", "")
BET_WEBAPP_TOKEN = os.getenv("BET_WEBAPP_TOKEN", "")

RAW_DIR = ROOT / "data" / "raw"
CLEAN_DIR = ROOT / "data" / "clean"
DB_PATH = ROOT / "db" / "mw_handicapping.duckdb"

RAW_DIR.mkdir(parents=True, exist_ok=True)
CLEAN_DIR.mkdir(parents=True, exist_ok=True)
(ROOT / "db").mkdir(parents=True, exist_ok=True)

# How far back to pull. 2016 gives a 10-season+ window; adjust freely.
# Every FBS game is pulled (not just MW) so opponent-adjusted ratings and
# newcomer history (UTEP/Northern Illinois) come along for free. See
# src/teams.py for North Dakota State's separate FCS-era pull.
START_YEAR = 2016
END_YEAR = 2026  # current season

def require_api_key():
    if not CFBD_API_KEY:
        raise RuntimeError(
            "No CFBD_API_KEY set. Copy .env.example to .env, grab a free key at "
            "https://collegefootballdata.com/key, and paste it in."
        )
    return CFBD_API_KEY
