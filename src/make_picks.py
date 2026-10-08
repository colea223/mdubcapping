"""
Weekly picks research sheet -- reads 12 matchups from a text file and builds
an Excel workbook with, for every matchup, a side-by-side breakdown of both
teams (Dub Gamma rating, SP+ offense/defense, SOR, SOS, season ATS record),
the market line, and the predicted spread from all four models (Gamma, Ridge,
XGBoost "Dub Beta", Massey). The player-matchup / scheme / storyline analysis
is yours to write -- each breakdown ends with blank notes boxes for it.

The 12 slots: 2 SEC, 2 Big Ten, 2 ACC, 2 Big 12, 2 Mountain West, 2 "other G6"
(American, Conference USA, MAC, Sun Belt, plus Pac-12 / FBS Independents).
A game involving two different slot conferences (e.g. a MW team @ a Big Ten
team) can fill either; the script finds an assignment that fills the quotas.

Input file (default: picks_input.txt in the project root), one matchup per
line, "Away @ Home" (team names as CFBD spells them; common aliases like
"San Jose State" work). Blank lines and lines starting with # are ignored:

    Navy @ Air Force
    UNLV @ Boise State
    ...

Pick side = Dub Gamma's lean vs the market (edge = market - model; lean Home
if edge > 0), the same rule the site/backtest use. Games without a posted
market line have no lean and are flagged.

Usage (from the project root, venv active -- after run_pipeline.py /
predict_week.py so the predictions CSV and market lines are fresh):
    python src/make_picks.py
    python src/make_picks.py my_picks.txt --week 7
Output: excel/picks/Week_<N>_Picks_<season>.xlsx
"""
import argparse
import csv
import re
import sys
import time
from pathlib import Path

import duckdb
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DB_PATH, CLEAN_DIR, ROOT  # noqa: E402
import backtest  # noqa: E402
import gamma_model  # noqa: E402
from predict_week import auto_detect_week  # noqa: E402
from teams import normalize_team_name  # noqa: E402

P4 = ["SEC", "Big Ten", "ACC", "Big 12"]
OTHER_G6 = {"American Athletic", "Conference USA", "Mid-American", "Sun Belt", "Pac-12", "FBS Independents"}
SLOTS = {"SEC": 2, "Big Ten": 2, "ACC": 2, "Big 12": 2, "Mountain West": 2, "Other G6": 2}
SLOT_ORDER = ["SEC", "Big Ten", "ACC", "Big 12", "Mountain West", "Other G6"]

NAVY = PatternFill("solid", fgColor="1F3864")
GREEN = PatternFill("solid", fgColor="C6EFCE")
GRAY = PatternFill("solid", fgColor="F2F2F2")
NOTE_FILL = PatternFill("solid", fgColor="FFF9E5")
WHITE_BOLD = Font(name="Arial", bold=True, color="FFFFFF", size=10)
BOLD = Font(name="Arial", bold=True, size=10)
BASE = Font(name="Arial", size=10)
TITLE = Font(name="Arial", bold=True, size=14)
SUB = Font(name="Arial", bold=True, size=12)
ITAL = Font(name="Arial", italic=True, size=9, color="666666")
THIN = Side(style="thin", color="BBBBBB")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def slot_of(conf):
    if conf in P4 or conf == "Mountain West":
        return conf
    if conf in OTHER_G6:
        return "Other G6"
    return None


def latest_predictions_csv(season, week):
    exact = CLEAN_DIR / f"week_{season}_{week}_predictions.csv"
    if exact.exists():
        return exact
    return None


def key(name):
    return re.sub(r"[^a-z0-9]", "", normalize_team_name(name).lower())


def parse_input(path):
    games = []
    for n, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s+(?:@|at)\s+|\s*@\s*", line, maxsplit=1, flags=re.I)
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            sys.exit(f"{path}:{n}: expected 'Away @ Home', got: {raw!r}")
        games.append((parts[0].strip(), parts[1].strip(), line))
    return games


def find_game(week_games, away_in, home_in):
    ak, hk = key(away_in), key(home_in)
    for g in week_games:
        if key(g["away_team"]) == ak and key(g["home_team"]) == hk:
            return g, False
    for g in week_games:   # same teams, reversed order
        if key(g["away_team"]) == hk and key(g["home_team"]) == ak:
            return g, True
    # loose substring match as a last resort
    cands = [g for g in week_games if ak in key(g["away_team"]) and hk in key(g["home_team"])]
    if len(cands) == 1:
        return cands[0], False
    return None, False


def assign_slots(picks):
    """Backtracking assignment of games to slot quotas. Candidate slots per
    game: the lean-side team's conference first, then the other team's."""
    quota = dict(SLOTS)
    cands = []
    for p in picks:
        order = []
        for conf in p["conf_order"]:
            s = slot_of(conf)
            if s and s not in order:
                order.append(s)
        cands.append(order)
    result = [None] * len(picks)

    def solve(i):
        if i == len(picks):
            return True
        for s in cands[i]:
            if quota[s] > 0:
                quota[s] -= 1
                result[i] = s
                if solve(i + 1):
                    return True
                quota[s] += 1
        return False

    if solve(0):
        return result, []
    # No perfect fill: greedy, then report what's off.
    quota = dict(SLOTS)
    result = [None] * len(picks)
    for i in range(len(picks)):
        for s in cands[i]:
            if quota[s] > 0:
                quota[s] -= 1
                result[i] = s
                break
    warnings = [f"Slot '{s}' has {q} open spot(s) unfilled" for s, q in quota.items() if q > 0]
    warnings += [f"'{picks[i]['label']}' doesn't fit any open slot" for i, r in enumerate(result) if r is None]
    return result, warnings


def rank_of(value_map, team, reverse=True):
    if team not in value_map or value_map[team] is None:
        return None
    vals = sorted((v for v in value_map.values() if v is not None), reverse=reverse)
    return vals.index(value_map[team]) + 1


def load_team_stats(con, season, week):
    stats = {}
    ratings, _ = gamma_model.replay_ratings(con, season=season, through_week=week - 1)
    stats["gamma"] = {normalize_team_name(t): v for t, v in ratings.items()}

    def safe(q, params=()):
        try:
            return con.execute(q, list(params)).fetchall()
        except Exception as e:   # table missing etc. -- leave that column blank
            print(f"  [warn] could not read data for a stats column ({str(e).splitlines()[0]})")
            return []

    sp = safe("SELECT team, rating, offense_rating, defense_rating FROM sp_ratings WHERE season = ?", [season])
    stats["sp_off"] = {normalize_team_name(t): o for t, r, o, d in sp}
    stats["sp_def"] = {normalize_team_name(t): d for t, r, o, d in sp}
    stats["sp_ovr"] = {normalize_team_name(t): r for t, r, o, d in sp}

    # SOR: latest sor_after per team this season (walk-forward: only games before this week)
    sor = safe("""
        SELECT team, sor_after FROM sor_baseline r JOIN games g ON g.game_id = r.game_id
        WHERE g.season = ? AND g.week < ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY team ORDER BY g.start_date DESC) = 1
    """, [season, week])
    stats["sor"] = {normalize_team_name(t): v for t, v in sor}

    sos = safe("SELECT team, avg_opponent_rating FROM sos_ratings WHERE season = ?", [season])
    stats["sos"] = {normalize_team_name(t): v for t, v in sos}

    # Season ATS vs the real market line, games before this week
    market = dict(safe("SELECT game_id, AVG(spread) FROM lines GROUP BY game_id"))
    ats = {}
    for gid, home, away, hp, ap in safe("""
        SELECT game_id, home_team, away_team, home_points, away_points FROM games
        WHERE season = ? AND week < ? AND completed = TRUE
          AND home_points IS NOT NULL AND away_points IS NOT NULL
    """, [season, week]):
        m = market.get(gid)
        if m is None:
            continue
        for team, margin, spread in ((home, hp - ap, m), (away, ap - hp, -m)):
            rec = ats.setdefault(normalize_team_name(team), [0, 0, 0])
            c = margin + spread
            if abs(c) < 1e-6:
                rec[2] += 1
            elif c > 0:
                rec[0] += 1
            else:
                rec[1] += 1
    stats["ats"] = ats

    # Last 3 completed games per team this season (before this week), with the
    # opponent's Dub Gamma rank as it stood ENTERING that game (walk-forward).
    rank_cache = {}

    def ranks_entering(wk):
        wk = max(wk - 1, 1)
        if wk not in rank_cache:
            r, _ = gamma_model.replay_ratings(con, season=season, through_week=wk)
            order = sorted(r, key=lambda t: r[t], reverse=True)
            rank_cache[wk] = {normalize_team_name(t): i + 1 for i, t in enumerate(order)}
        return rank_cache[wk]

    recent = {}
    rows = safe("""
        SELECT week, start_date, home_team, away_team, home_points, away_points, neutral_site, game_id FROM games
        WHERE season = ? AND week < ? AND completed = TRUE
          AND home_points IS NOT NULL AND away_points IS NOT NULL
        ORDER BY start_date
    """, [season, week])
    for wk, sd, home, away, hp, ap, neutral, gid in rows:
        m = market.get(gid)
        for team, opp, pts, opp_pts, site, sp in (
                (home, away, hp, ap, "vs" if not neutral else "N", m),
                (away, home, ap, hp, "@" if not neutral else "N", -m if m is not None else None)):
            recent.setdefault(normalize_team_name(team), []).append({
                "week": wk, "opp": opp, "site": site, "pts": pts, "opp_pts": opp_pts,
                "opp_rank": ranks_entering(wk).get(normalize_team_name(opp)),
                "spread": round(sp, 1) if sp is not None else None,   # this team's market line (negative = favored)
            })
    stats["recent"] = {t: g[-3:][::-1] for t, g in recent.items()}   # most recent first
    return stats


def team_block(stats, team):
    t = normalize_team_name(team)
    g = stats["gamma"].get(t)
    rec = stats["ats"].get(t)
    out = {
        "gamma": g, "gamma_rank": rank_of(stats["gamma"], t),
        "sp_off": stats["sp_off"].get(t), "sp_off_rank": rank_of(stats["sp_off"], t),
        "sp_def": stats["sp_def"].get(t), "sp_def_rank": rank_of(stats["sp_def"], t, reverse=False),
        "sor": stats["sor"].get(t), "sor_rank": rank_of(stats["sor"], t),
        "sos": stats["sos"].get(t), "sos_rank": rank_of(stats["sos"], t),
        "ats": f"{rec[0]}-{rec[1]}-{rec[2]}" if rec else "n/a",
        "recent": stats["recent"].get(t, []),
        "ats_pct": (rec[0] / (rec[0] + rec[1])) if rec and (rec[0] + rec[1]) else None,
    }
    return out


def fmt_line(spread_home):
    """Home-perspective spread -> 'Home -3.5' style text is built by caller."""
    return None if spread_home is None else round(float(spread_home), 1)


def side_text(home, away, home_spread):
    if home_spread is None:
        return "n/a"
    if home_spread == 0:
        return "Pick'em"
    if home_spread < 0:
        return f"{home} {home_spread:+.1f}"
    return f"{away} {-home_spread:+.1f}"


def build_workbook(season, week, picks, warnings, out_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Picks"
    ws.sheet_view.showGridLines = False
    ws["A1"] = f"Dub Handicapping -- Week {week} Picks ({season})"
    ws["A1"].font = TITLE
    ws["A2"] = ("Pick = Dub Gamma's lean vs the market. Edge = Market - Gamma line (home perspective, "
                f"negative line = home favored); green = edge >= {backtest.EDGE_THRESHOLD:g} pts.")
    ws["A2"].font = ITAL

    headers = ["#", "Slot", "Date", "Matchup (Away @ Home)", "Pick", "Pick Line", "Market (Home)",
               "Gamma", "Ridge", "XGBoost (Beta)", "Massey", "Edge (pts)", "Model Total", "Market Total",
               "Win/Loss", "Result"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=c, value=h)
        cell.font = WHITE_BOLD
        cell.fill = NAVY
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[4].height = 30

    for i, p in enumerate(picks, start=1):
        r = 4 + i
        vals = [i, p["slot"], p["date"], f"{p['away']} @ {p['home']}", p["pick_team"] or "No market line",
                p["pick_line"], p["market"], p["gamma"], p["ridge"], p["xgb"], p["massey"],
                p["edge"], p["model_total"], p["market_total"], None, None]
        for c, v in enumerate(vals, start=1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.font = BASE
            cell.border = BOX
            if c in (1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14):
                cell.alignment = Alignment(horizontal="center")
        ws.cell(row=r, column=5).font = BOLD
        if p["edge"] is not None and abs(p["edge"]) >= backtest.EDGE_THRESHOLD:
            ws.cell(row=r, column=12).fill = GREEN
        # Win/Loss left for you to fill after the game (data-validation list).
    from openpyxl.worksheet.datavalidation import DataValidation
    dv = DataValidation(type="list", formula1='"W,L,P"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(f"O5:O{4 + len(picks)}")

    widths = [4, 15, 11, 38, 24, 12, 13, 9, 9, 13, 9, 10, 11, 11, 9, 30]
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w

    note_row = 6 + len(picks)
    if warnings:
        ws.cell(row=note_row, column=1, value="Warnings").font = SUB
        for k, w in enumerate(warnings, start=1):
            ws.cell(row=note_row + k, column=1, value=f"- {w}").font = BASE

    # --- Breakdowns sheet: one stacked block per pick ---
    bs = wb.create_sheet("Breakdowns")
    bs.sheet_view.showGridLines = False
    for col, w in zip("ABCDEFG", [30, 22, 10, 22, 10, 10, 12]):
        bs.column_dimensions[col].width = w
    r = 1
    for i, p in enumerate(picks, start=1):
        bs.cell(row=r, column=1, value=f"{i}. {p['slot']}: {p['away']} @ {p['home']}  ({p['date']})").font = SUB
        r += 1
        bs.cell(row=r, column=1,
                value=f"Pick: {p['pick_team'] or 'no market line yet'}"
                      + (f" {p['pick_line']:+.1f}" if p["pick_line"] is not None else "")
                      + (f"   |   Edge {p['edge']:+.1f} pts" if p["edge"] is not None else "")).font = BOLD
        r += 2

        # team comparison table
        for c, h in enumerate(["", p["away"] + " (Away)", "Rank", p["home"] + " (Home)", "Rank"], start=1):
            cell = bs.cell(row=r, column=c, value=h)
            cell.font = WHITE_BOLD
            cell.fill = NAVY
            cell.alignment = Alignment(horizontal="center", wrap_text=True)
        r += 1
        a, h = p["away_stats"], p["home_stats"]

        def n1(v):
            return None if v is None else round(float(v), 1)

        rows = [
            ("Overall Rating (Dub Gamma)", n1(a["gamma"]), a["gamma_rank"], n1(h["gamma"]), h["gamma_rank"]),
            ("Offensive Rating (SP+ offense)", n1(a["sp_off"]), a["sp_off_rank"], n1(h["sp_off"]), h["sp_off_rank"]),
            ("Defensive Rating (SP+ defense, lower = better)", n1(a["sp_def"]), a["sp_def_rank"], n1(h["sp_def"]), h["sp_def_rank"]),
            ("SOR (strength of record)", n1(a["sor"]), a["sor_rank"], n1(h["sor"]), h["sor_rank"]),
            ("SOS (avg opponent Elo)", n1(a["sos"]), a["sos_rank"], n1(h["sos"]), h["sos_rank"]),
            ("ATS record, season (W-L-P)", a["ats"], None, h["ats"], None),
        ]
        for lbl, av, ar, hv, hr in rows:
            for c, v in enumerate([lbl, av, ar, hv, hr], start=1):
                cell = bs.cell(row=r, column=c, value=v)
                cell.font = BOLD if c == 1 else BASE
                cell.border = BOX
                if c > 1:
                    cell.alignment = Alignment(horizontal="center")
            r += 1
        r += 1

        # last 3 games for each team
        for team, st in ((p["away"], a), (p["home"], h)):
            for c, hd in enumerate([f"{team}: last 3 games", "Opponent (rank entering)", "Wk", "Score", "Result", "Spread", "ATS"], start=1):
                cell = bs.cell(row=r, column=c, value=hd)
                cell.font = WHITE_BOLD
                cell.fill = NAVY
                cell.alignment = Alignment(horizontal="center", wrap_text=True)
            r += 1
            if not st["recent"]:
                bs.cell(row=r, column=1, value="No completed games before this week").font = ITAL
                r += 1
            for gm in st["recent"]:
                rk = f"#{gm['opp_rank']}" if gm["opp_rank"] else "NR"
                res = "W" if gm["pts"] > gm["opp_pts"] else ("L" if gm["pts"] < gm["opp_pts"] else "T")
                if gm["spread"] is None:
                    ats_txt = "n/a"
                else:
                    cm = (gm["pts"] - gm["opp_pts"]) + gm["spread"]
                    ats_txt = "Push" if abs(cm) < 1e-6 else ("Cover" if cm > 0 else "No Cover")
                sp_txt = None if gm["spread"] is None else ("PK" if gm["spread"] == 0 else f"{gm['spread']:+.1f}")
                vals = [f"{gm['site']} {gm['opp']}", rk, gm["week"], f"{gm['pts']}-{gm['opp_pts']}", res, sp_txt, ats_txt]
                for c, v in enumerate(vals, start=1):
                    cell = bs.cell(row=r, column=c, value=v)
                    cell.font = BASE
                    cell.border = BOX
                    if c > 1:
                        cell.alignment = Alignment(horizontal="center")
                bs.cell(row=r, column=5).fill = GREEN if res == "W" else PatternFill("solid", fgColor="FFC7CE")
                if ats_txt in ("Cover", "No Cover"):
                    bs.cell(row=r, column=7).fill = GREEN if ats_txt == "Cover" else PatternFill("solid", fgColor="FFC7CE")
                r += 1
            r += 1

        # lines table
        for c, hd in enumerate(["Lines (home perspective)", "Spread (Home)", "", "Favors", ""], start=1):
            cell = bs.cell(row=r, column=c, value=hd)
            cell.font = WHITE_BOLD
            cell.fill = NAVY
            cell.alignment = Alignment(horizontal="center")
        r += 1
        line_rows = [("Market (Vegas)", p["market"]), ("Dub Gamma (live)", p["gamma"]),
                     ("Ridge", p["ridge"]), ("XGBoost (Dub Beta)", p["xgb"]), ("Massey", p["massey"])]
        for lbl, sp in line_rows:
            vals = [lbl, sp, None, side_text(p["home"], p["away"], sp), None]
            for c, v in enumerate(vals, start=1):
                cell = bs.cell(row=r, column=c, value=v)
                cell.font = BOLD if (c == 1 or lbl.startswith("Market")) else BASE
                cell.border = BOX
                if c > 1:
                    cell.alignment = Alignment(horizontal="center")
            r += 1
        tot = [("Total: model / market", f"{p['model_total']} / {p['market_total']}")]
        for lbl, v in tot:
            bs.cell(row=r, column=1, value=lbl).font = BASE
            bs.cell(row=r, column=2, value=v).font = BASE
        r += 2

        # your notes
        for label in ["Player matchups", "Scheme / tendencies", "Storylines / situational", "Final take"]:
            bs.cell(row=r, column=1, value=label).font = BOLD
            bs.merge_cells(start_row=r, start_column=2, end_row=r, end_column=5)
            nc = bs.cell(row=r, column=2)
            nc.fill = NOTE_FILL
            nc.alignment = Alignment(wrap_text=True, vertical="top")
            bs.row_dimensions[r].height = 42
            r += 1
        r += 2

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", nargs="?", default=str(ROOT / "picks_input.txt"))
    ap.add_argument("--week", type=int)
    ap.add_argument("--season", type=int)
    args = ap.parse_args()

    if not Path(args.input).exists():
        sys.exit(f"Input file not found: {args.input}\nCreate it with one 'Away @ Home' matchup per line (12 total).")
    wanted = parse_input(args.input)
    if len(wanted) != 12:
        print(f"[note] {len(wanted)} matchup(s) in {args.input} (expected 12) -- continuing anyway.")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    if args.week:
        season = args.season or gamma_model.SEED_SEASON
        week = args.week
    else:
        det = auto_detect_week(con)
        if det is None:
            sys.exit("No upcoming week found in the DB.")
        season, week = det[0], det[1]
    print(f"Season {season}, week {week}")

    week_games = [dict(zip(("game_id", "away_team", "home_team", "date", "neutral", "home_conf", "away_conf"), row))
                  for row in con.execute("""
        SELECT game_id, away_team, home_team, start_date, neutral_site, home_conference, away_conference
        FROM games WHERE season = ? AND week = ?""", [season, week]).fetchall()]

    pred_path = latest_predictions_csv(season, week)
    preds = {}
    if pred_path:
        with open(pred_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("Game ID"):
                    preds[int(row["Game ID"])] = row
    else:
        print(f"[warn] {CLEAN_DIR / f'week_{season}_{week}_predictions.csv'} not found -- run src/predict_week.py first; "
              "Ridge/XGBoost/Massey/total columns will be blank.")

    market = {gid: (s, t) for gid, s, t in
              con.execute("SELECT game_id, AVG(spread), AVG(over_under) FROM lines GROUP BY game_id").fetchall()}

    stats = load_team_stats(con, season, week)
    warnings = []
    picks = []
    seen = set()
    for away_in, home_in, raw in wanted:
        g, reversed_ = find_game(week_games, away_in, home_in)
        if g is None:
            warnings.append(f"Could not find '{raw}' in week {week} -- check spelling/home-away.")
            continue
        if reversed_:
            warnings.append(f"'{raw}' is actually {g['away_team']} @ {g['home_team']} -- used the real home/away.")
        if g["game_id"] in seen:
            warnings.append(f"Duplicate matchup: {raw}")
            continue
        seen.add(g["game_id"])

        pr = preds.get(int(g["game_id"]), {})

        def num(col):
            v = pr.get(col)
            try:
                return round(float(v), 1) if v not in (None, "") else None
            except ValueError:
                return None

        gamma, ridge, xgb, massey = (num("Model Line (Home)"), num("Ridge Line (Home)"),
                                     num("XGBoost Line (Home)"), num("Massey Line (Home)"))
        model_total = num("Model Total")
        if gamma is None:   # no predictions CSV row: Gamma is cheap to compute directly
            gr = stats["gamma"]
            h_, a_ = normalize_team_name(g["home_team"]), normalize_team_name(g["away_team"])
            if h_ in gr and a_ in gr:
                gamma = round(gamma_model.predict_spread_home(gr, h_, a_, neutral_site=bool(g["neutral"])), 1)
        m_spread, m_total = market.get(int(g["game_id"]), (None, None))
        m_spread = round(m_spread, 1) if m_spread is not None else None
        m_total = round(m_total, 1) if m_total is not None else None
        if not pr:
            warnings.append(f"{g['away_team']} @ {g['home_team']}: no row in predictions CSV (Gamma computed directly; "
                            "Ridge/XGBoost/Massey/total blank -- rerun predict_week.py).")
        if m_spread is None:
            warnings.append(f"{g['away_team']} @ {g['home_team']}: no market line posted yet -- no lean.")

        edge = pick_team = pick_line = None
        conf_order = [g["home_conf"], g["away_conf"]]
        if m_spread is not None and gamma is not None:
            edge = round(m_spread - gamma, 1)
            if edge != 0:
                lean_home = edge > 0
                pick_team = g["home_team"] if lean_home else g["away_team"]
                pick_line = m_spread if lean_home else -m_spread
                conf_order = [g["home_conf"], g["away_conf"]] if lean_home else [g["away_conf"], g["home_conf"]]
            else:
                pick_team = "Pick'em (no edge)"
        picks.append({
            "label": f"{g['away_team']} @ {g['home_team']}", "away": g["away_team"], "home": g["home_team"],
            "date": str(g["date"])[:10], "conf_order": conf_order,
            "pick_team": pick_team, "pick_line": pick_line, "market": m_spread,
            "gamma": gamma, "ridge": ridge, "xgb": xgb, "massey": massey, "edge": edge,
            "model_total": model_total, "market_total": m_total,
            "away_stats": team_block(stats, g["away_team"]), "home_stats": team_block(stats, g["home_team"]),
        })
    con.close()

    slots, slot_warn = assign_slots(picks)
    warnings += slot_warn
    for p, s in zip(picks, slots):
        p["slot"] = s or "Unassigned"
    picks_sorted = sorted(zip(picks, slots), key=lambda t: (SLOT_ORDER.index(t[1]) if t[1] in SLOT_ORDER else 99))
    picks = [p for p, _ in picks_sorted]

    out = ROOT / "excel" / "picks" / f"Week_{week}_Picks_{season}.xlsx"
    build_workbook(season, week, picks, warnings, out)
    print(f"\nWrote {out}")
    for w in warnings:
        print(f"  [warn] {w}")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"[Finished in {time.time() - t0:.1f}s]")
