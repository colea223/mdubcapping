"""
Player value model: how many POINTS OF SPREAD does a team lose when a given player is out?

Used by gamma_model.load_injury_weights_by_week() (pre-game spread adjustment AND the injury term in Gamma's
rating update), and by pull_injury_news.py to decide whose headlines to scan.

THE MODEL (lineup-based, so backups and linemen count):
  1. Every rostered player gets a QUALITY q in [0, 1] -- a percentile among players at his position group
     nationwide -- built from whatever evidence exists:
        production : season PPA per team game (skill players, QBs, some defenders), else his snap share
                     ("usage", from CFBD) -- both ranked within his position group
        talent     : recruiting composite rating (data/clean/player_talent.csv, from pull_player_talent.py);
                     the only nationwide per-player measure for offensive/defensive linemen and for
                     players who haven't played yet (i.e. backups)
     q = 0.65*production + 0.35*talent when both exist; production alone; 0.85*talent alone (a prior, shrunk);
     0.20 when nothing is known (a below-average player).
  2. A player's worth in points is  POSITION_BASE[pos] * q**2  (squared so value concentrates in real stars).
  3. A team's lineup strength in a position group is the sum of its best N healthy players there (a backup who steps in counts at 60%) (QB 1, RB 1, WR 3,
     TE 1, OL 5, DL 4, LB 3, DB 4, K 1, P 1). The cost of an injury is the DROP in that sum when the player
     is removed: lose a star QB and a 4-star backup steps in -> small drop; the backup is a walk-on -> big
     drop. Losing a bench player costs nothing; losing two of five starting linemen costs both slots.
  4. Uncertain statuses are weighted by their probability (Out 1.0, Doubtful 0.75, Questionable 0.5,
     Probable 0.25); Out/Doubtful teammates are treated as already missing when valuing another injury.

HONEST LIMITS: POSITION_BASE values are priors from widely used betting/analytics rules of thumb, not fitted to
this project's data (no historical player-availability data exists to fit them on). Recruiting rating is a weak
predictor for veterans, which is why production gets the larger weight whenever it exists. Offensive linemen have no
public production stat, so they are rated on recruiting rating alone. Name matching is by team + name.
"""
import csv
import re

import numpy as np

from config import CLEAN_DIR
from teams import normalize_team_name

POSITION_BASE = {
    "QB": 7.0,
    "RB": 2.0, "FB": 0.8,
    "WR": 1.8,
    "TE": 1.0,
    "OL": 1.5, "OT": 1.5, "OG": 1.5, "C": 1.5, "G": 1.5, "T": 1.5, "IOL": 1.5,
    "DL": 1.5, "DE": 1.5, "EDGE": 1.5, "DT": 1.3, "NT": 1.0,
    "LB": 1.1, "ILB": 1.1, "OLB": 1.2,
    "DB": 1.2, "CB": 1.4, "S": 1.0, "FS": 1.0, "SS": 1.0, "SAF": 1.0,
    "ATH": 1.0, "K": 0.5, "PK": 0.5, "P": 0.3,
}
DEFAULT_BASE = 1.0
GROUP_OF = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE",
            "OL": "OL", "OT": "OL", "OG": "OL", "C": "OL", "G": "OL", "T": "OL", "IOL": "OL",
            "DL": "DL", "DE": "DL", "EDGE": "DL", "DT": "DL", "NT": "DL",
            "LB": "LB", "ILB": "LB", "OLB": "LB",
            "DB": "DB", "CB": "DB", "S": "DB", "FS": "DB", "SS": "DB", "SAF": "DB",
            "K": "K", "PK": "K", "P": "P"}
SLOTS = {"QB": 1, "RB": 1, "WR": 3, "TE": 1, "OL": 5, "DL": 4, "LB": 3, "DB": 4, "K": 1, "P": 1}

QUALITY_POWER = 2.0
MIN_TOTAL_PPA = 1.0          # below this season-to-date total a player has no production signal from PPA
MIN_USAGE = 0.02             # below this snap share a player has no production signal from usage
PROD_WEIGHT, TALENT_WEIGHT = 0.65, 0.35
TALENT_ONLY_SHRINK = 0.85
UNKNOWN_Q = 0.20
DEPTH_FACTOR = 0.6           # a backup stepping into the lineup plays at 60% of his rating (he was not the starter
#                              for a reason; ratings alone can't see that)
OUT_THRESHOLD = 0.75         # injuries at/above this probability count as "already out" for teammates' valuation

TALENT_CSV = CLEAN_DIR / "player_talent.csv"
USAGE_CSV = CLEAN_DIR / "player_usage.csv"


def _first_pos(position):
    if not position:
        return ""
    return re.split(r"[/,\s-]+", str(position).strip().upper())[0]


def name_key(name):
    return re.sub(r"[^a-z ]", "", (name or "").lower()).strip()


def last_key(name):
    parts = name_key(name).split()
    return parts[-1] if parts else ""


def _read_csv(path, season):
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return [r for r in csv.DictReader(f) if str(r.get("season")) == str(season)]
    except Exception:  # noqa: BLE001 - optional data
        return []


def _percentiles(values):
    """values: {key: number} -> {key: percentile in (0,1]} (needs >=5 values, else {})."""
    if len(values) < 5:
        return {}
    arr = np.sort(np.array(list(values.values()), dtype=float))
    return {k: float(np.searchsorted(arr, v, side="right")) / len(arr) for k, v in values.items()}


def build_player_values(con, season):
    """Returns {"by_full", "by_last", "by_team", "lineups"}:
        by_full  {(team, full_name_key): info}      by_last {(team, last_name_key): info}
        by_team  {team: [info sorted by standalone loss desc]}
        lineups  {(team, group): [info sorted by value desc]}
    info = {name, team, position, group, q, value (points when worn alone), loss (points if removed alone)}."""
    empty = {"by_full": {}, "by_last": {}, "by_team": {}, "lineups": {}}

    games_by_team = {}
    try:
        for home, away in con.execute(
            "SELECT home_team, away_team FROM games WHERE season = ? AND completed = TRUE", [season]
        ).fetchall():
            for t in (home, away):
                k = normalize_team_name(t)
                games_by_team[k] = games_by_team.get(k, 0) + 1
    except Exception:  # noqa: BLE001
        pass

    players = {}                       # (team, name_key) -> dict

    def entry(team, name, position):
        key = (normalize_team_name(team), name_key(name))
        p = players.get(key)
        if p is None:
            p = players[key] = {"name": name, "team": key[0], "pos_raw": "", "ppg": None, "usage": None,
                                "recruit": None}
        pos = _first_pos(position)
        if pos and not p["pos_raw"]:
            p["pos_raw"] = pos
        return p

    for r in _read_csv(TALENT_CSV, season):
        p = entry(r["team"], r["name"], r["position"])
        if r.get("recruit_rating") not in (None, ""):
            p["recruit"] = float(r["recruit_rating"])
    for r in _read_csv(USAGE_CSV, season):
        p = entry(r["team"], r["name"], r["position"])
        try:
            p["usage"] = float(r["usage_overall"])
        except (TypeError, ValueError):
            pass
    try:
        rows = con.execute(
            "SELECT player_name, position, team, total_ppa FROM player_season_ppa WHERE season = ?", [season]
        ).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    for name, pos, team, total in rows:
        if not name or total is None or float(total) < MIN_TOTAL_PPA:
            continue
        p = entry(team, name, pos)
        p["ppg"] = float(total) / max(games_by_team.get(p["team"], 0), 1)

    # Position group + percentiles within group.
    by_group = {}
    for key, p in players.items():
        g = GROUP_OF.get(p["pos_raw"])
        if g is None:
            continue
        p["group"] = g
        by_group.setdefault(g, []).append(key)
    infos = []
    for g, keys in by_group.items():
        ppa_pct = _percentiles({k: players[k]["ppg"] for k in keys if players[k]["ppg"] is not None})
        use_pct = _percentiles({k: players[k]["usage"] for k in keys
                                if players[k]["usage"] is not None and players[k]["usage"] >= MIN_USAGE})
        rec_pct = _percentiles({k: players[k]["recruit"] for k in keys if players[k]["recruit"] is not None})
        for k in keys:
            p = players[k]
            prod = ppa_pct.get(k, use_pct.get(k))
            tal = rec_pct.get(k)
            if prod is not None and tal is not None:
                q = PROD_WEIGHT * prod + TALENT_WEIGHT * tal
            elif prod is not None:
                q = prod
            elif tal is not None:
                q = TALENT_ONLY_SHRINK * tal
            else:
                q = UNKNOWN_Q
            base = POSITION_BASE.get(p["pos_raw"], DEFAULT_BASE)
            infos.append({"name": p["name"], "team": p["team"], "position": p["pos_raw"], "group": g,
                          "q": round(q, 3), "value": round(base * q ** QUALITY_POWER, 3), "loss": 0.0})

    lineups = {}
    for i in infos:
        lineups.setdefault((i["team"], i["group"]), []).append(i)
    for (team, g), lst in lineups.items():
        lst.sort(key=lambda x: -x["value"])
        n = SLOTS[g]
        nxt = lst[n]["value"] if len(lst) > n else 0.0
        for idx, i in enumerate(lst):
            i["loss"] = round(i["value"] - DEPTH_FACTOR * nxt, 3) if idx < n else 0.0

    by_full, by_last, by_team = {}, {}, {}
    for i in infos:
        by_team.setdefault(i["team"], []).append(i)
        for table, key in ((by_full, (i["team"], name_key(i["name"]))), (by_last, (i["team"], last_key(i["name"])))):
            if key not in table or i["loss"] > table[key]["loss"] or (
                    i["loss"] == table[key]["loss"] and i["value"] > table[key]["value"]):
                table[key] = i
    for t in by_team:
        by_team[t].sort(key=lambda x: (-x["loss"], -x["value"]))
    return {"by_full": by_full, "by_last": by_last, "by_team": by_team, "lineups": lineups}


def find_player(values, team, player_name):
    team_n = normalize_team_name(team)
    return (values["by_full"].get((team_n, name_key(player_name)))
            or values["by_last"].get((team_n, last_key(player_name))))


def lookup_value(values, team, player_name):
    """Points lost if this one player is out (0.0 if unknown or a non-starter)."""
    info = find_player(values, team, player_name)
    return info["loss"] if info else 0.0


def _lineup_sum(lst, removed_ids, n):
    """lst sorted by value desc (the depth chart). Top n healthy players count; anyone who was not originally in
    the top n counts at DEPTH_FACTOR of his rating."""
    total, taken = 0.0, 0
    for rank, i in enumerate(lst):
        if id(i) in removed_ids:
            continue
        total += i["value"] * (1.0 if rank < n else DEPTH_FACTOR)
        taken += 1
        if taken == n:
            break
    return total


def team_injury_burden(values, team, injured):
    """injured: [(player_name, probability_out)] for ONE team-week -> expected points of spread lost.
    Each player's cost is measured with his Out/Doubtful teammates already removed from the lineup."""
    team_n = normalize_team_name(team)
    resolved = []
    for name, mult in injured:
        info = find_player(values, team_n, name)
        if info is not None and mult > 0:
            if not any(info is r[0] for r in resolved):
                resolved.append((info, mult))
    total = 0.0
    for info, mult in resolved:
        g = info["group"]
        lst = values["lineups"].get((team_n, g), [])
        n = SLOTS[g]
        others = {id(o) for o, m in resolved if o is not info and o["group"] == g and m >= OUT_THRESHOLD}
        drop = _lineup_sum(lst, others, n) - _lineup_sum(lst, others | {id(info)}, n)
        total += mult * drop
    return total
