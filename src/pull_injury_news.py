"""
Second, independent injury source: scans recent NEWS HEADLINES for every FBS team's key players.

WHY: covers.com's injury page misses players (it did not list Oregon QB Dante Moore at all). Headlines
from ESPN/Yahoo/SI/etc. report "ruled out vs. X" and "out for the season" within hours, so this script looks
for exactly that wording next to a KEY player's full name.

HOW IT WORKS
  1. For each FBS team, take its few most valuable players (src/player_values.py, e.g. the starting QB).
  2. Query Google News' public RSS search for that player + injury words (last LOOKBACK_DAYS days).
  3. Classify each headline conservatively (see classify_text): Out for season / Out / Doubtful /
     Questionable, or Active (cleared / will play). Questions ("Will X play?"), picks/odds/draft-stock
     headlines and anything where the injury wording is not next to the player's name are ignored.
  4. Map each headline to that team's next game (so "ruled out vs. Illinois" lands in the right week) and
     append it to data/clean/injury_news.csv (append-only, de-duplicated). The latest headline wins; an
     "Out for season" status stays in force for later weeks until a later headline says the player is
     back (see load_news_injuries).

LIMITS / HONEST NOTES
  * Headlines are noisy. Rules are strict on purpose (a missed injury is better than a phantom one), and
    every detection is printed so you can audit it. Only a team's top players are scanned.
  * Google News' RSS terms describe personal, non-commercial use. This script makes ~1 request per scanned
    player, politely throttled. If that ever becomes a problem for your use case, swap fetch_rss() for
    another news source -- nothing else depends on it.
  * It never stops the pipeline: any failure prints a warning and the run continues.

Usage:
    python src/pull_injury_news.py
"""
import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CLEAN_DIR  # noqa: E402
from player_values import build_player_values, name_key  # noqa: E402
from teams import normalize_team_name, FBS_CONFERENCES  # noqa: E402

NEWS_CSV = CLEAN_DIR / "injury_news.csv"
COLUMNS = ["detected_at", "season", "team", "player", "status", "sticky", "pub_date", "week", "headline"]
LOOKBACK_DAYS = 21
PLAYERS_PER_TEAM = 4
MIN_PLAYER_VALUE = 0.75      # only scan players worth at least this many spread points
MAX_WORKERS = 4
REQUEST_PAUSE = 0.4
LAST_WEEK = 20

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"
SKIP_TITLE = re.compile(r"\b(picks?|predictions?|odds|betting|props?|fantasy|draft (stock|prediction|rank)|mock draft|"
                        r"nfl draft|power rankings?|recap|reacts?|fans)\b", re.I)
QUESTION_START = re.compile(r"^(will|is|are|when|should|can|could|how|what|why|does|do|did|who)\b", re.I)

ACTIVE_RE = re.compile(r"\b(cleared to play|cleared for|will play|expected to play|set to return|"
                       r"returns? (to|for|vs\.?|against|from)|is back|back in the lineup|good to go|full go|activated)\b", re.I)
SEASON_RE = re.compile(r"(out for (?:the )?(?:rest of )?(?:this |the )?(?:\w+ )?(?:season|year)\b|"
                       r"rest of (?:the |this )?(?:\d{4} )?(?:season|year)|season[- ]ending|"
                       r"(?:done|lost) for the (?:season|year)|out the rest of)", re.I)
OUT_RE = re.compile(r"\b(ruled out|will not play|won't play|will miss|to miss|misses|missing|"
                    r"out (?:vs\.?|against|for|on|saturday|friday|thursday|this)|sidelined|suspended|"
                    r"out indefinitely|out injured|remains? out|still out|is out|are out|out due to)\b", re.I)
DOUBTFUL_RE = re.compile(r"\bdoubtful\b", re.I)
QUESTIONABLE_RE = re.compile(r"\b(questionable|game[- ]?time decision|day[- ]to[- ]day|week[- ]to[- ]week|"
                             r"uncertain for|status (?:unclear|uncertain)|in doubt)\b", re.I)


def classify_text(text):
    """-> (status, sticky) or None."""
    if ACTIVE_RE.search(text):
        return "Active", False
    if SEASON_RE.search(text):
        return "Out for season", True
    if OUT_RE.search(text):
        return "Out", False
    if DOUBTFUL_RE.search(text):
        return "Doubtful", False
    if QUESTIONABLE_RE.search(text):
        return "Questionable", False
    return None


def split_segments(title):
    return [s.strip() for s in re.split(r"\s*(?:[,;:|–—]|\s-\s)\s*", title) if s.strip()]


def classify_title(title, names):
    """names: list of player full names to look for. Returns [(player, status, sticky)]. The injury wording must
    sit in the same comma/colon-separated segment as the player's name, or in the segment right after a short
    list of names ("Dante Moore, Evan Stewart ruled out vs. Illinois")."""
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()       # drop trailing " - Source"
    if not t or t.endswith("?") or QUESTION_START.match(t) or SKIP_TITLE.search(t):
        return []
    segs = split_segments(t)
    found = {}
    for i, seg in enumerate(segs):
        verdict = classify_text(seg)
        if not verdict:
            continue
        targets = [n for n in names if re.search(r"\b" + re.escape(n) + r"\b", seg, re.I)]
        j = i - 1
        while j >= 0 and len(segs[j].split()) <= 4 and not classify_text(segs[j]):
            targets += [n for n in names if n not in targets and re.search(r"\b" + re.escape(n) + r"\b", segs[j], re.I)]
            j -= 1
        for n in targets:
            found.setdefault(n, verdict)
    return [(n, v[0], v[1]) for n, v in found.items()]


def fetch_rss(query):
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    time.sleep(REQUEST_PAUSE)
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
    if r.status_code == 429:
        raise RuntimeError("rate limited (HTTP 429)")
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate")).astimezone(timezone.utc)
        except Exception:
            continue
        items.append((it.findtext("title") or "", pub))
    return items


def team_game_weeks(con, season):
    out = {}
    for week, start, home, away in con.execute("""
        SELECT week, start_date, home_team, away_team FROM games
        WHERE season = ? AND start_date IS NOT NULL ORDER BY start_date
    """, [season]).fetchall():
        start = start.replace(tzinfo=timezone.utc) if start.tzinfo is None else start
        for t in (home, away):
            out.setdefault(normalize_team_name(t), []).append((start, int(week)))
    return out


def week_for(game_weeks, team, pub):
    for start, wk in game_weeks.get(team, []):
        if start >= pub - timedelta(hours=36):
            return wk
    return None


def scan_player(task):
    team, player, query = task
    try:
        return team, player, fetch_rss(query), None
    except Exception as e:  # noqa: BLE001 - keep scanning other players
        return team, player, [], f"{type(e).__name__}: {e}"


def read_existing():
    if not NEWS_CSV.exists():
        return []
    with open(NEWS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    try:
        _main()
    except Exception as e:  # noqa: BLE001
        print(f"  [injury news] step failed and was skipped: {type(e).__name__}: {e}")


def _main():
    import duckdb
    from config import DB_PATH
    start = time.time()
    con = duckdb.connect(str(DB_PATH))
    season = con.execute("SELECT MAX(season) FROM games").fetchone()[0]
    values = build_player_values(con, season)
    if not values["by_team"]:
        print("  [injury news] no player values yet (player_season_ppa empty) -- skipped.")
        return
    game_weeks = team_game_weeks(con, season)
    fbs = {normalize_team_name(r[0]) for r in con.execute(
        f"SELECT home_team FROM games WHERE season = {int(season)} AND home_conference IN "
        f"({','.join(repr(c) for c in FBS_CONFERENCES)})").fetchall()}

    tasks = []
    for team in sorted(fbs):
        for p in [x for x in values["by_team"].get(team, []) if x["loss"] >= MIN_PLAYER_VALUE][:PLAYERS_PER_TEAM]:
            q = (f'"{p["name"]}" {team} (injury OR "ruled out" OR "out for" OR questionable OR doubtful) '
                 f'when:{LOOKBACK_DAYS}d')
            tasks.append((team, p["name"], q))
    print(f"  [injury news] scanning {len(tasks)} key players across {len(fbs)} FBS teams ...")

    existing = read_existing()
    seen = {(r["team"], r["player"], r["pub_date"], r["status"]) for r in existing}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)
    new_rows, errors = [], 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for team, player, items, err in ex.map(scan_player, tasks):
            if err:
                errors += 1
                if errors <= 3:
                    print(f"    warn: {player} ({team}): {err}")
                continue
            for title, pub in items:
                if pub < cutoff:
                    continue
                for pname, status, sticky in classify_title(title, [player]):
                    wk = week_for(game_weeks, team, pub)
                    if wk is None:
                        continue
                    key = (team, pname, pub.strftime("%Y-%m-%d %H:%M"), status)
                    if key in seen:
                        continue
                    seen.add(key)
                    new_rows.append({"detected_at": now.strftime("%Y-%m-%d %H:%M"), "season": season, "team": team,
                                     "player": pname, "status": status, "sticky": int(sticky),
                                     "pub_date": key[2], "week": wk, "headline": title[:200]})
    if errors:
        print(f"  [injury news] {errors} request(s) failed (partial results kept).")
    NEWS_CSV.parent.mkdir(parents=True, exist_ok=True)
    if new_rows or not NEWS_CSV.exists():
        with open(NEWS_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(existing + new_rows)
    print(f"  [injury news] {len(new_rows)} new headline detection(s); {len(existing) + len(new_rows)} total on file.")

    # Audit print: current standing status per player (latest wins) for the recent window.
    latest = {}
    for r in sorted(existing + new_rows, key=lambda r: r["pub_date"]):
        latest[(r["team"], r["player"])] = r
    shown = [r for r in latest.values() if r["status"] != "Active" and r["pub_date"] >= (now - timedelta(days=10)).strftime("%Y-%m-%d")]
    for r in sorted(shown, key=lambda r: (r["team"], r["player"])):
        print(f"    {r['team']:<18} {r['player']:<22} {r['status']:<15} wk{r['week']}  | {r['headline'][:90]}")
    print(f"  [injury news] done in {time.time() - start:.0f}s")


def load_news_injuries(season):
    """Rows [(week, team, player_name, status)] in the same shape as injury_reports, resolved from
    data/clean/injury_news.csv: latest headline wins per player; 'Out for season' stays in force for every
    later week until a later 'Active' headline; other statuses apply to their own week only."""
    try:
        rows = [r for r in read_existing() if int(r["season"]) == season]
    except Exception:  # noqa: BLE001
        return []
    by_player = {}
    for r in sorted(rows, key=lambda r: r["pub_date"]):
        by_player.setdefault((r["team"], r["player"]), []).append(r)
    out = []
    for (team, player), events in by_player.items():
        weekmap = {}
        for e in events:
            wk = int(e["week"])
            if e["status"] == "Active":
                for w in [w for w in weekmap if w >= wk]:
                    del weekmap[w]
            elif e["sticky"] in ("1", 1, True):
                for w in range(wk, LAST_WEEK + 1):
                    weekmap[w] = e["status"]
            else:
                weekmap[wk] = e["status"]
        out += [(w, team, player, s) for w, s in weekmap.items()]
    return out


if __name__ == "__main__":
    main()
