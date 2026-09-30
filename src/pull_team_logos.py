"""
Pulls every FBS team's logo image URL from CFBD's own /teams endpoint
(Team.logos -- a list of externally-hosted, hotlinkable PNG URLs CFBD itself
serves for exactly this purpose) and writes a raw snapshot the same way every
other pull_*.py script does, for build_db.py's build_team_logos_table() to
load.

Logos essentially never change season to season, so unlike pull_games.py/
pull_stats.py this doesn't need to run on every pipeline refresh -- run it
once, and again only if a team rebrands or a new program joins FBS. It's
listed in run_pipeline.py's STEPS anyway (cheap: one CFBD call) so it just
stays current for free.

Usage:
    source .venv/bin/activate
    python src/pull_team_logos.py
"""
from datetime import datetime, timezone

from config import RAW_DIR, END_YEAR
from cfbd_client import get_api_client, teams_api
from raw_storage import write_json_gz, prune_superseded


def main():
    client = get_api_client()
    api = teams_api(client)
    teams = api.get_teams(year=END_YEAR)

    rows = []
    for t in teams:
        if not t.logos:
            continue
        rows.append({"school": t.school, "conference": t.conference, "logo_url": t.logos[0]})

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RAW_DIR / f"team_logos_{END_YEAR}_{stamp}.json"
    written = write_json_gz(path, rows)
    removed = prune_superseded(RAW_DIR, f"team_logos_{END_YEAR}_*.json*", written)
    print(f"team_logos: pulled {len(rows)} team logo(s) -> {written.name}"
          + (f" (pruned {len(removed)} older snapshot(s))" if removed else ""))


if __name__ == "__main__":
    main()
