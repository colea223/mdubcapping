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
from datetime import datetime, timezone

from config import RAW_DIR
from cfbd_client import get_api_client, teams_api
from raw_storage import write_json_gz, prune_superseded
import cfbd


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main():
    client = get_api_client()
    api = cfbd.VenuesApi(client)
    stamp = _stamp()

    print("Pulling venue metadata...")
    venues = api.get_venues()
    # .dict(by_alias=False), not .to_dict() -- consistent with the other pull
    # scripts (see pull_games.py's comment). Venue field names happen to
    # mostly avoid the camelCase-alias mismatch already, but keeping every
    # raw snapshot on the same snake_case convention avoids surprises later.
    out_path = write_json_gz(RAW_DIR / f"venues_static_{stamp}.json", [v.dict(by_alias=False) for v in venues])
    print(f"  -> {len(venues)} venues -> {out_path.name}")
    # No year component in this filename (venues don't change season to
    # season) -- only ever the latest pull matters, so prune every older one.
    removed = prune_superseded(RAW_DIR, "venues_static_*.json*", out_path)
    if removed:
        print(f"  pruned {len(removed)} superseded snapshot(s): {removed}")
    print("\nDone. Re-run this occasionally (a few times a season is plenty) -- new venues are rare.")


if __name__ == "__main__":
    main()
