"""
Pulls the data that lets the player-value model rate EVERY player on a roster -- linemen and backups included,
not just the skill players CFBD publishes PPA for.

Writes (both are committed by the workflow with the rest of data/clean/):
  data/clean/player_talent.csv  season, team, name, position, recruit_rating, stars
      One row per rostered FBS player. recruit_rating is the player's best recruiting composite rating
      (HS / prep / JUCO classes) linked through the roster's recruit_ids -- the only nationwide, per-player
      talent measure that exists for offensive and defensive linemen and for players who haven't played yet.
      Refreshed at most every ROSTER_REFRESH_DAYS days (rosters and recruiting classes barely change in-season).
  data/clean/player_usage.csv   season, team, name, position, usage_overall
      CFBD's share of team plays each player is on the field for; identifies who actually starts. Every run.

Never stops the pipeline: any failure prints a warning and the old files (if any) stay in place.
"""
import csv
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CLEAN_DIR, END_YEAR  # noqa: E402

TALENT_CSV = CLEAN_DIR / "player_talent.csv"
USAGE_CSV = CLEAN_DIR / "player_usage.csv"
ROSTER_REFRESH_DAYS = 7
RECRUIT_CLASSES_BACK = 6          # roster players come from the last ~6 recruiting classes
CLASSIFICATIONS = ("HighSchool", "JUCO", "PrepSchool")


def _fresh(path, days):
    return path.exists() and datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < timedelta(days=days)


def _fbs_team_names(client, year):
    import cfbd
    return sorted({t.school for t in cfbd.TeamsApi(client).get_fbs_teams(year=year) if t.school})


def pull_talent(client, year):
    import cfbd
    teams_api = cfbd.TeamsApi(client)
    rec_api = cfbd.RecruitingApi(client)

    ratings = {}                       # recruit id -> (rating, stars)
    for cls in range(year - RECRUIT_CLASSES_BACK, year + 1):
        for c in CLASSIFICATIONS:
            try:
                for r in rec_api.get_recruits(year=cls, classification=cfbd.RecruitClassification(c)):
                    if r.id is not None and r.rating is not None:
                        ratings[str(r.id)] = (float(r.rating), r.stars)
            except Exception as e:  # noqa: BLE001 - a missing class/classification is fine
                print(f"    recruits {cls} {c}: {type(e).__name__}")
            time.sleep(0.2)
    print(f"  [talent] {len(ratings)} recruit ratings loaded")

    roster = []
    try:                               # one bulk call if the API allows it ...
        roster = teams_api.get_roster(year=year, classification=cfbd.DivisionClassification.FBS)
    except Exception:  # noqa: BLE001
        roster = []
    if len(roster) < 3000:             # ... otherwise one call per team
        roster = []
        for school in _fbs_team_names(client, year):
            try:
                roster += teams_api.get_roster(team=school, year=year)
            except Exception as e:  # noqa: BLE001
                print(f"    roster {school}: {type(e).__name__}")
            time.sleep(0.15)
    print(f"  [talent] {len(roster)} rostered players")

    rows = []
    for p in roster:
        name = f"{p.first_name or ''} {p.last_name or ''}".strip()
        if not name or not p.team:
            continue
        best = None
        for rid in (p.recruit_ids or []):
            hit = ratings.get(str(rid))
            if hit and (best is None or hit[0] > best[0]):
                best = hit
        rows.append({"season": year, "team": p.team, "name": name, "position": p.position or "",
                     "recruit_rating": "" if best is None else round(best[0], 4),
                     "stars": "" if best is None or best[1] is None else best[1]})
    if len(rows) < 2000:
        raise RuntimeError(f"only {len(rows)} roster rows -- not overwriting {TALENT_CSV.name}")
    with open(TALENT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["season", "team", "name", "position", "recruit_rating", "stars"])
        w.writeheader()
        w.writerows(rows)
    with_rating = sum(1 for r in rows if r["recruit_rating"] != "")
    print(f"  [talent] wrote {len(rows)} players ({with_rating} with a recruiting rating) -> {TALENT_CSV.name}")


def pull_usage(client, year):
    import cfbd
    items = cfbd.PlayersApi(client).get_player_usage(year=year, exclude_garbage_time=True)
    rows = []
    for u in items:
        usage = getattr(u, "usage", None)
        overall = getattr(usage, "overall", None) if usage is not None else None
        if overall is None and isinstance(usage, dict):
            overall = usage.get("overall")
        if u.name and u.team and overall is not None:
            rows.append({"season": year, "team": u.team, "name": u.name, "position": u.position or "",
                         "usage_overall": round(float(overall), 4)})
    if not rows:
        print("  [talent] no usage rows returned -- keeping previous file")
        return
    with open(USAGE_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["season", "team", "name", "position", "usage_overall"])
        w.writeheader()
        w.writerows(rows)
    print(f"  [talent] wrote {len(rows)} usage rows -> {USAGE_CSV.name}")


def main():
    try:
        from cfbd_client import get_api_client
        client = get_api_client()
    except Exception as e:  # noqa: BLE001
        print(f"  [talent] skipped (no API client): {type(e).__name__}: {e}")
        return
    for label, fn, skip in (("roster + recruiting talent", pull_talent, _fresh(TALENT_CSV, ROSTER_REFRESH_DAYS)),
                            ("player usage", pull_usage, False)):
        if skip:
            print(f"  [talent] {label}: file is fresh, skipped")
            continue
        try:
            fn(client, END_YEAR)
        except Exception as e:  # noqa: BLE001
            print(f"  [talent] {label} failed and was skipped: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
