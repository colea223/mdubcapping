"""
Weekly college football injury report, scraped from covers.com (see
src/gamma_model.py's own docstring for why this exists at all -- no CFBD
endpoint or paid feed was worth using, see that file's design notes).

covers.com/sport/football/ncaaf/injuries is a single page listing every FBS
team's current injury report, server-rendered (no JavaScript needed) and not
blocked by covers.com's own robots.txt for this path. It is NOT an official
API -- there's no public contract that this stays parseable, and scraping a
commercial site's page on a schedule sits in a legal gray area even where
robots.txt happens to allow it (see this project's own notes/README on the
tradeoff). Kept deliberately light: one page fetch per pipeline run, nothing
more.

WHAT IT GIVES US, AND WHAT IT DOESN'T: covers.com publishes a status
("Out", "Questionable - Knee", etc.) per player, but only a first initial
plus last name (e.g. "D. Hubbard") -- never a full first name. That's not
enough to join to CFBD's player data by name alone if two players on the
same team share a last name (rare, but possible) -- see match_player() in
gamma_model.py for how that gets resolved (last name + team + position,
falling back to "first match" with a printed warning if more than one
candidate remains).

STORAGE: unlike most raw snapshots (which are "latest wins," see
raw_storage.py's own docstring), this one is intentionally kept PER WEEK,
same pattern src/pull_stats.py's pull_current_week_ppa_snapshot() already
established for ppa_snapshots -- gamma_model.py's walk-forward replay needs
to look back at what a team's injury picture was entering week N specifically
when it grades week N's games later, not whatever this week's report says.
Filename: injury_report_w<NN>_<season>_<stamp>.json[.gz]; build_db.py's
build_injury_reports_table() groups by that embedded week number exactly the
way it already does for ppa_snapshots.

Usage:
    source .venv/bin/activate
    python src/pull_injuries.py
"""
import time
from datetime import datetime, timezone, date

import cfbd
import requests
from bs4 import BeautifulSoup

from config import RAW_DIR, END_YEAR
from cfbd_client import get_api_client, games_api
from raw_storage import write_json_gz, prune_superseded
from teams import normalize_team_name

INJURIES_URL = "https://www.covers.com/sport/football/ncaaf/injuries"

# A realistic desktop User-Agent -- covers.com serves this page to plain
# requests fine (confirmed: 200, full HTML, no JS needed), but a default
# python-requests UA is a common, cheap bot-blocking trigger on commercial
# sites, so this avoids tripping that for no reason.
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"

# Graduated weight (fraction of the player's season PPA that counts as
# "lost") per Cole's own scale -- Out is a full loss, lesser designations
# count proportionally less. Matched against the FRONT of covers.com's
# status text (e.g. "Questionable - Knee" matches "questionable"), case-
# insensitive, longest-key-first so "day-to-day" doesn't get shadowed by a
# shorter unrelated prefix. Anything covers.com prints that isn't in this
# table falls back to 0.5 (the middle of the scale) rather than 0 --
# treating an unrecognized designation as "playing at full strength" would
# quietly throw away real signal, and it's printed loudly (see main()) so a
# new wording covers.com starts using doesn't sit unnoticed.
STATUS_WEIGHTS = {
    "out": 1.0,
    "ir": 1.0,
    "injured reserve": 1.0,
    "out for season": 1.0,
    "out for the season": 1.0,
    "suspended": 1.0,
    "doubtful": 0.75,
    "questionable": 0.5,
    "day-to-day": 0.5,
    "day to day": 0.5,
    "probable": 0.25,
}
DEFAULT_STATUS_WEIGHT = 0.5


def status_weight(status_text: str):
    """Returns (weight, matched_label) for a raw covers.com status string,
    or (DEFAULT_STATUS_WEIGHT, None) if nothing in STATUS_WEIGHTS matched
    (None signals "unrecognized" to main()'s summary print, distinct from a
    real questionable-style 0.5 match)."""
    low = status_text.strip().lower()
    for label in sorted(STATUS_WEIGHTS, key=len, reverse=True):
        if low.startswith(label):
            return STATUS_WEIGHTS[label], label
    return DEFAULT_STATUS_WEIGHT, None


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _detect_current_week(client, season: int):
    """
    Deliberately calendar-based (GamesApi.get_calendar), NOT a query against
    the local `games` table -- same trick src/pull_stats.py's
    pull_current_week_ppa_snapshot() already uses, and for the same reason:
    this script has to run BEFORE build_db.py has loaded this run's raw
    pulls (its own output only gets INTO the `injury_reports` table via the
    next build_db.py call), so the local DB's `games` table may still be
    stale or, on a fresh clone, not exist yet. Asking CFBD's own calendar
    instead sidesteps that ordering problem entirely.

    Picks the earliest REGULAR season week whose games aren't all in the
    past yet (last_game_start on or after today) -- a close approximation
    of predict_week.py's DB-based auto_detect_week(), good enough here
    since this is just picking which week to tag the snapshot with, not a
    completed/incomplete precision requirement.
    """
    games = games_api(client)
    calendar = games.get_calendar(year=season)
    today = date.today()
    candidates = sorted(
        (c.week, c.last_game_start.date())
        for c in calendar
        if c.season_type == cfbd.SeasonType.REGULAR and c.last_game_start.date() >= today
    )
    return candidates[0][0] if candidates else None


def fetch_injury_page() -> str:
    resp = requests.get(INJURIES_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_injury_page(html: str):
    """
    Returns a list of dicts: {team, player_initial, player_last_name,
    position, status}. `team` is run through normalize_team_name() so it
    lines up with every other table's team spelling.

    Structure (see this file's own module docstring / gamma_model.py for
    background): each team gets a <div class="...blockContainer"> with a
    team-name div and an injury table; "no injuries" teams have a single
    stray <td>No injuries to report.</td> with no real <tr>, which the
    row-scan below naturally produces zero rows for.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for block in soup.select("div.covers-CoversSeasonInjuries-blockContainer"):
        name_div = block.select_one("div.covers-CoversMatchups-teamName")
        if not name_div:
            continue
        # teamName's own text is "<a>Team<br><span>Mascot</span></a>" -- the
        # team name is the <a> tag's leading NavigableString, BEFORE the
        # <br> (not the <span>'s own previous_sibling, which is the <br>
        # tag itself, not the text -- easy to get backwards here).
        mascot_span = name_div.find("span")
        parent_a = mascot_span.find_parent("a") if mascot_span else None
        team_raw = (str(parent_a.contents[0]) if parent_a and parent_a.contents
                    else name_div.get_text(strip=True))
        team_raw = team_raw.strip()
        if not team_raw:
            continue
        team = normalize_team_name(team_raw)

        for tr in block.select("table.covers-CoversMatchups-Table tbody tr"):
            if "collapse" in (tr.get("class") or []):
                continue  # the hidden per-player description row, not a player row
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue  # the "No injuries to report." placeholder row
            name_span = tds[0].find("span", class_="player-link")
            name_text = " ".join((name_span or tds[0]).get_text(" ", strip=True).split())
            if not name_text:
                continue
            parts = name_text.split(" ", 1)
            initial, last_name = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
            position = tds[1].get_text(strip=True)
            status_b = tds[2].find("b")
            status_text = (status_b.get_text(strip=True) if status_b
                            else tds[2].get_text(" ", strip=True).split("(")[0].strip())
            if not status_text:
                continue
            rows.append({
                "team": team,
                "player_initial": initial,
                "player_last_name": last_name,
                "position": position,
                "status": status_text,
            })
    return rows


def main():
    client = get_api_client()
    week = _detect_current_week(client, END_YEAR)
    if week is None:
        print(f"No upcoming {END_YEAR} regular-season week found on CFBD's calendar yet "
              "(before the season's first game, or between seasons). Skipping injury report pull.")
        return

    print(f"Fetching covers.com injury report for {END_YEAR} week {week}...")
    html = fetch_injury_page()
    rows = parse_injury_page(html)
    print(f"  -> {len(rows)} injury listings across {len({r['team'] for r in rows})} teams")

    unrecognized = sorted({r["status"] for r in rows if status_weight(r["status"])[1] is None})
    if unrecognized:
        print(f"  NOTE: {len(unrecognized)} status wording(s) not in STATUS_WEIGHTS, defaulted to "
              f"{DEFAULT_STATUS_WEIGHT} -- consider adding them: {unrecognized}")

    stamp = _stamp()
    prefix = f"injury_report_w{week:02d}"
    path = write_json_gz(RAW_DIR / f"{prefix}_{END_YEAR}_{stamp}.json", rows)
    print(f"  -> {path.name}")
    # "Latest wins" WITHIN a week only -- a same-week re-run (e.g. a manual
    # re-trigger) shouldn't leave two snapshots of the SAME week around, but
    # different weeks' files must never be pruned against each other (the
    # glob below is scoped to this exact week's prefix, not injury_report_*
    # generally -- same trick build_ppa_snapshots_table()'s PPA_SNAPSHOT_
    # PREFIX_RE comment already explains for ppa_snapshot_w<NN>).
    removed = prune_superseded(RAW_DIR, f"{prefix}_{END_YEAR}_*.json*", path)
    if removed:
        print(f"  pruned {len(removed)} superseded same-week snapshot(s): {removed}")


if __name__ == "__main__":
    _script_start_time = time.time()
    main()
    print(f"\nDone in {time.time() - _script_start_time:.1f}s.")
