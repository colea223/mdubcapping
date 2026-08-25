"""
Pull venue metadata (lat/long/elevation/dome/grass) once -- venues don't change
year to year, so unlike games/stats/lines this isn't a per-season pull. This is
what powers the travel-distance and elevation-delta features in features.py
(the altitude edge at Air Force/New Mexico/Wyoming, and travel for everyone
visiting Hawai'i).

Usage:
    source .venv/bin/activate
    python src/pull_venues.py
"""
import json
from datetime import datetime, timezone

from config import RAW_DIR
from cfbd_client import get_api_client, teams_api
import cfbd


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main():
    client = get_api_client()
    api = cfbd.VenuesApi(client)
    stamp = _stamp()

    print("Pulling venue metadata...")
    venues = api.get_venues()
    out_path = RAW_DIR / f"venues_static_{stamp}.json"
    out_path.write_text(json.dumps([v.to_dict() for v in venues], indent=2, default=str))
    print(f"  -> {len(venues)} venues -> {out_path.name}")
    print("\nDone. Re-run this occasionally (a few times a season is plenty) -- new venues are rare.")


if __name__ == "__main__":
    main()