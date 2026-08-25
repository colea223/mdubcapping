"""
Pull advanced (PPA/EPA) season stats, SP+ ratings, Elo, and recruiting talent
composites for every season in [START_YEAR, END_YEAR]. These are the "best
measure" inputs discussed in the attack plan -- efficiency margin as the
primary signal, SP+/Elo as an external blend/sanity-check, recruiting as the
prior for thin-sample teams (this year: UTEP, Northern Illinois, North Dakota
State).

Usage:
    source .venv/bin/activate
    python src/pull_stats.py
"""
import json
from datetime import datetime, timezone

from config import START_YEAR, END_YEAR, RAW_DIR
from cfbd_client import get_api_client, stats_api, ratings_api, recruiting_api


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def main():
    client = get_api_client()
    stats = stats_api(client)
    ratings = ratings_api(client)
    recruiting = recruiting_api(client)
    stamp = _stamp()

    for year in range(START_YEAR, END_YEAR + 1):
        print(f"Pulling {year} advanced season stats (PPA)...")
        adv = stats.get_advanced_season_stats(year=year)
        (RAW_DIR / f"advanced_stats_{year}_{stamp}.json").write_text(
            json.dumps([a.to_dict() for a in adv], indent=2, default=str)
        )
        print(f"  -> {len(adv)} teams")

        print(f"Pulling {year} SP+ ratings...")
        sp = ratings.get_sp(year=year)
        (RAW_DIR / f"sp_ratings_{year}_{stamp}.json").write_text(
            json.dumps([s.to_dict() for s in sp], indent=2, default=str)
        )
        print(f"  -> {len(sp)} teams")

        print(f"Pulling {year} Elo ratings...")
        elo = ratings.get_elo(year=year)
        (RAW_DIR / f"elo_ratings_{year}_{stamp}.json").write_text(
            json.dumps([e.to_dict() for e in elo], indent=2, default=str)
        )
        print(f"  -> {len(elo)} teams")

        print(f"Pulling {year} recruiting composite...")
        rec = recruiting.get_team_recruiting_rankings(year=year)
        (RAW_DIR / f"recruiting_{year}_{stamp}.json").write_text(
            json.dumps([r.to_dict() for r in rec], indent=2, default=str)
        )
        print(f"  -> {len(rec)} teams")

    print("\nDone. Raw snapshots written to data/raw/.")


if __name__ == "__main__":
    main()
