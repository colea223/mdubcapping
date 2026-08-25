"""
Pull betting lines from CFBD (spotty before ~2015, decent recent seasons).
Run this TWICE per week during the season and keep both snapshots -- once
right after lines open, once right before kickoff -- since the gap between
opening and closing lines is exactly what CLV (closing line value) backtesting
measures. The timestamp in the filename keeps every pull distinct; nothing here
overwrites a prior snapshot.

CFBD's own line coverage has real gaps historically -- for full historical
closing lines (older seasons), supplement with a manually downloaded archive
(e.g. Sports Book Review Online season files) dropped into data/raw/ as
lines_external_<year>.csv; pull_lines.py only handles the CFBD side.

Usage:
    source .venv/bin/activate
    python src/pull_lines.py
"""
import json
from datetime import datetime, timezone

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, betting_api


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main():
    client = get_api_client()
    api = betting_api(client)
    stamp = _stamp()

    for year in range(START_YEAR, END_YEAR + 1):
        print(f"Pulling {year} betting lines...")
        lines = api.get_lines(year=year)
        out_path = RAW_DIR / f"lines_{year}_{stamp}.json"
        out_path.write_text(json.dumps([l.to_dict() for l in lines], indent=2, default=str))
        print(f"  -> {len(lines)} games with line data -> {out_path.name}")

    print("\nDone. Raw snapshots written to data/raw/.")
    print("Reminder: re-run this weekly (opening + closing) once the season starts.")


if __name__ == "__main__":
    main()
