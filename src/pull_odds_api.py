"""
Pull live college football odds from The Odds API (the-odds-api.com) -- a
second, independent line feed alongside CFBD's pull_lines.py, on its own
schedule and its own quota (default Sun/Wed/Fri -- see
.github/workflows/odds_pull.yml).

Why a second source at all: CFBD's lines endpoint only ever returns two
points per (game, book) -- current and open, never a real intraday history.
This script's whole job is to add more points along that timeline between
full-pipeline runs, so the Line History chart on the website shows real
market movement instead of just however often the CFBD pipeline happened to
run. It is DISPLAY ONLY -- see build_odds_api_snapshots_table() in
build_db.py -- nothing here ever touches model.py/backtest.py, which stay on
CFBD's `lines` table exactly as before.

Cost: The Odds API charges (markets requested) x (regions requested) credits
per call, NOT per game returned -- the one call below (3 markets x 1 region
= 3 credits) returns every NCAAF game in one shot, MW or not; games that
aren't yours are simply skipped downstream in build_db.py. At 3 credits per
pull, running this 3x/week costs ~39 credits/month against the free tier's
500 -- comfortable headroom even if you run it more often.

Setup:
    1. Get a free key at https://the-odds-api.com/
    2. Put it in .env as ODDS_API_KEY=... (locally) and/or as a GitHub
       Actions repo secret named ODDS_API_KEY (for the scheduled workflow)

Usage:
    source .venv/bin/activate
    python src/pull_odds_api.py
"""
from datetime import datetime, timezone

import requests

from config import RAW_DIR, END_YEAR, ODDS_API_KEY
from raw_storage import write_json_gz

API_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_ncaaf/odds"
REGIONS = "us"
MARKETS = "spreads,totals,h2h"


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def require_odds_api_key():
    if not ODDS_API_KEY:
        raise RuntimeError(
            "No ODDS_API_KEY set. Copy .env.example to .env (or add a repo secret "
            "for GitHub Actions), grab a free key at https://the-odds-api.com/, "
            "and paste it in."
        )
    return ODDS_API_KEY


def main():
    key = require_odds_api_key()
    resp = requests.get(
        API_URL,
        params={
            "apiKey": key,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": "american",
            "dateFormat": "iso",
        },
        timeout=30,
    )
    resp.raise_for_status()
    events = resp.json()

    stamp = _stamp()
    # Compression only -- NEVER pruned. build_db.py's build_odds_api_snapshots_table()
    # deliberately scans EVERY historical snapshot for the current season to
    # power the website's Line History chart, so every timestamped pull here
    # has to survive; see raw_storage.py's module docstring.
    out_path = write_json_gz(RAW_DIR / f"odds_api_{END_YEAR}_{stamp}.json", events)
    print(f"Pulled {len(events)} NCAAF games from The Odds API -> {out_path.name}")

    # The Odds API reports quota usage in response headers, not the body.
    used = resp.headers.get("x-requests-used")
    remaining = resp.headers.get("x-requests-remaining")
    if used is not None:
        print(f"Total credits used this billing period: {used}")
    if remaining is not None:
        print(f"Credits remaining: {remaining}")

    print(
        "\nDone. Run 'python src/build_db.py' next to fold this into "
        "line_snapshots -- it only keeps games that match something already "
        "in your `games` table for the current season, so run pull_games.py "
        "first if this is a brand-new week."
    )


if __name__ == "__main__":
    main()
