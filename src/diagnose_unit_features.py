"""
Walk-forward A/B test: do opponent-adjusted unit ratings (run game, pass game,
pass block/rush, run/pass defense -- the same stats make_picks.py shows) help
the Ridge margin model?

Variants (all Ridge, same walk-forward protocol as backtest.py):
  A  baseline  = model.FEATURE_COLS
  B  A + 12 unit features (6 stats x offense/defense difference)
  C  A + 4 PPA-only unit features (rush/pass PPA, offense/defense difference)

Every unit feature for a game is computed ONLY from plays in the same season in
weeks BEFORE that game's week (no look-ahead), opponent-adjusted and shrunk
toward 0 by play count. Regular-season games only. Models are refit each week
on all earlier games. Reports ATS (uses backtest.grade_spread_pick, so the
1-pt Vegas-favorite default applies), MAE, and a paired comparison.

Usage:  python src/diagnose_unit_features.py
Read-only on the database; writes data/clean/unit_feature_ab.csv.
"""
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DB_PATH, CLEAN_DIR  # noqa: E402
import model  # noqa: E402
import backtest  # noqa: E402
from teams import normalize_team_name  # noqa: E402

DROPBACK = {"Pass Reception", "Pass Incompletion", "Passing Touchdown", "Sack",
            "Pass Interception Return", "Interception", "Interception Return Touchdown", "Pass Completion"}
RUSH = {"Rush", "Rushing Touchdown"}
INT = {"Pass Interception Return", "Interception", "Interception Return Touchdown"}
STATS = ["rush_ppa", "db_ppa", "sack_rate", "rush_succ", "db_succ", "pd_succ"]
PPA_STATS = ["rush_ppa", "db_ppa"]
SHRINK_K = 150.0          # plays of "prior" pulling each adjusted value toward league average
TEST_SEASONS = [2019, 2021, 2022, 2023, 2024, 2025]
FIRST_TEST_WEEK = 4


def build_game_stat_table(con):
    df = con.execute("""
        SELECT p.game_id, g.season, g.week, p.offense, p.defense, p.down, p.distance,
               p.yards_gained, p.play_type, p.ppa
        FROM plays p JOIN games g ON g.game_id = p.game_id
        WHERE g.season_type = 'regular'
    """).fetchdf()
    df["offense"] = df["offense"].map(normalize_team_name)
    df["defense"] = df["defense"].map(normalize_team_name)
    df["is_rush"] = df["play_type"].isin(RUSH)
    df["is_db"] = df["play_type"].isin(DROPBACK)
    df = df[df["is_rush"] | df["is_db"]].copy()
    need = np.where(df["down"] == 1, 0.5 * df["distance"],
                    np.where(df["down"] == 2, 0.7 * df["distance"], df["distance"]))
    df["success"] = ((df["yards_gained"] >= need) & ~df["play_type"].isin(INT)
                     & (df["play_type"] != "Sack")).astype(float)
    pdm = ((df["down"] == 2) & (df["distance"] >= 8)) | (df["down"].isin([3, 4]) & (df["distance"] >= 5))
    ok = df["ppa"].notna()
    z = lambda m: m.astype(float)
    cols = {
        "rush_ppa": (df["ppa"].where(df["is_rush"] & ok, 0), z(df["is_rush"] & ok)),
        "db_ppa": (df["ppa"].where(df["is_db"] & ok, 0), z(df["is_db"] & ok)),
        "sack_rate": (z(df["play_type"] == "Sack"), z(df["is_db"])),
        "rush_succ": (df["success"].where(df["is_rush"], 0), z(df["is_rush"])),
        "db_succ": (df["success"].where(df["is_db"], 0), z(df["is_db"])),
        "pd_succ": (df["success"].where(df["is_db"] & pdm, 0), z(df["is_db"] & pdm)),
    }
    base = df[["game_id", "season", "week", "offense", "defense"]].reset_index(drop=True)
    tables = {}
    for name, (num, den) in cols.items():
        t = base.assign(num=num.values, den=den.values)
        tables[name] = t.groupby(["game_id", "season", "week", "offense", "defense"], as_index=False)[["num", "den"]].sum()
    return tables


def adjusted(tg):
    """tg: rows (offense, defense, num, den) for plays before the cutoff.
    Returns (off_adj, def_adj): Series indexed by team, shrunk opponent-adjusted
    deviation from league average."""
    tg = tg[tg["den"] > 0]
    if tg.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)
    league = tg["num"].sum() / tg["den"].sum()
    tot_off = tg.groupby("offense")[["num", "den"]].sum()
    tot_def = tg.groupby("defense")[["num", "den"]].sum()

    out = {}
    for side, key, opp_key, opp_tot in (("off", "offense", "defense", tot_def), ("def", "defense", "offense", tot_off)):
        t = tg.copy()
        o = opp_tot.reindex(t[opp_key])
        on = o["num"].values - t["num"].values
        od = o["den"].values - t["den"].values
        with np.errstate(divide="ignore", invalid="ignore"):
            shift = np.where(od > 0, t["den"].values * (on / od - league), 0.0)
        t = t.assign(shift=shift)
        g = t.groupby(key)[["num", "den", "shift"]].sum()
        raw = g["num"] / g["den"]
        adj = raw - g["shift"] / g["den"] - league          # deviation from league, schedule-adjusted
        adj = adj * g["den"] / (g["den"] + SHRINK_K)
        out[side] = adj
    return out["off"], out["def"]


def unit_features_for(tables, season, week, games_wk):
    """games_wk: DataFrame(game_id, home_team, away_team). Returns DataFrame of unit features."""
    feats = pd.DataFrame({"game_id": games_wk["game_id"].values})
    home = games_wk["home_team"].map(normalize_team_name).values
    away = games_wk["away_team"].map(normalize_team_name).values
    for name in STATS:
        t = tables[name]
        tg = t[(t["season"] == season) & (t["week"] < week)]
        off, dfn = adjusted(tg)
        feats[f"u_{name}_off"] = off.reindex(home).values - off.reindex(away).values
        feats[f"u_{name}_def"] = dfn.reindex(away).values - dfn.reindex(home).values
    return feats


def main():
    con = duckdb.connect(str(DB_PATH), read_only=True)
    print("Building play tables ...")
    tables = build_game_stat_table(con)
    full = model.load_training_frame(con)
    meta = con.execute("SELECT game_id, season, week, season_type FROM games").fetchdf()
    full = full.merge(meta[["game_id", "season_type"]], on="game_id", suffixes=("", "_m"))
    full = full[full["season_type"] == "regular"].copy()
    full["season"] = full["season"].astype(int)
    full["week"] = full["week"].astype(int)

    print("Computing walk-forward unit features ...")
    parts = []
    for (s, w), grp in full.groupby(["season", "week"]):
        parts.append(unit_features_for(tables, s, w, grp[["game_id", "home_team", "away_team"]]))
    full = full.merge(pd.concat(parts), on="game_id", how="left")

    unit_cols = [c for c in full.columns if c.startswith("u_")]
    ppa_cols = [f"u_{n}_{k}" for n in PPA_STATS for k in ("off", "def")]
    variants = {"A_baseline": model.FEATURE_COLS,
                "B_plus_12_units": model.FEATURE_COLS + unit_cols,
                "C_plus_PPA_units": model.FEATURE_COLS + ppa_cols}

    rows = []
    full = full.sort_values("start_date")
    for s in TEST_SEASONS:
        weeks = sorted(full[(full["season"] == s)]["week"].unique())
        for w in weeks:
            if w < FIRST_TEST_WEEK:
                continue
            test = full[(full["season"] == s) & (full["week"] == w)]
            test = test[test["market_spread_home"].notna()]
            if test.empty:
                continue
            cutoff = full[(full["season"] == s) & (full["week"] == w)]["start_date"].min()
            train = full[full["start_date"] < cutoff]
            for vname, cols in variants.items():
                pipe = model.build_pipeline()
                pipe.fit(train[cols], train["margin"])
                pred = pipe.predict(test[cols])
                for gid, p, mk, mg, wk in zip(test["game_id"], pred, test["market_spread_home"], test["margin"], test["week"]):
                    gr = backtest.grade_spread_pick(float(-p), float(mk), None, float(mg), backtest.EDGE_THRESHOLD)
                    rows.append({"variant": vname, "season": s, "week": int(wk), "game_id": gid,
                                 "pred_margin": p, "margin": mg, "market": mk,
                                 "result": gr["bet_result"], "is_bet": gr["is_bet"]})
        print(f"  season {s} done")

    res = pd.DataFrame(rows)
    res.to_csv(Path(CLEAN_DIR) / "unit_feature_ab.csv", index=False)

    def summarize(df, label):
        print(f"\n=== {label} ===")
        out = []
        for v, g in df.groupby("variant"):
            gg = g[g["result"].isin(["Win", "Loss"])]
            rb = gg[gg["is_bet"]]
            out.append({"variant": v, "games": len(gg),
                        "ATS%": round((gg["result"] == "Win").mean() * 100, 2),
                        "real_edge_n": len(rb),
                        "real_edge_ATS%": round((rb["result"] == "Win").mean() * 100, 2) if len(rb) else np.nan,
                        "MAE_margin": round((g["pred_margin"] - g["margin"]).abs().mean(), 3),
                        "MAE_vs_market": round((g["pred_margin"] + g["market"]).abs().mean(), 3)})
        print(pd.DataFrame(out).to_string(index=False))

    summarize(res, f"All test weeks (wk >= {FIRST_TEST_WEEK}), seasons {TEST_SEASONS}")
    summarize(res[res["week"] >= 6], "Week 6 and later only")
    # paired: B vs A on identical games
    for other in ("B_plus_12_units", "C_plus_PPA_units"):
        a = res[res.variant == "A_baseline"].set_index("game_id")
        b = res[res.variant == other].set_index("game_id")
        j = a[["result"]].join(b[["result"]], lsuffix="_a", rsuffix="_b").dropna()
        j = j[j.result_a.isin(["Win", "Loss"]) & j.result_b.isin(["Win", "Loss"])]
        a_only = ((j.result_a == "Win") & (j.result_b == "Loss")).sum()
        b_only = ((j.result_a == "Loss") & (j.result_b == "Win")).sum()
        print(f"\nPaired {other} vs baseline: {len(j)} games, differing outcomes {a_only + b_only} "
              f"(baseline-only wins {a_only}, new-only wins {b_only})")
    print("\nA gain is only convincing if it holds in both slices AND beats the noise: with ~"
          "5,000 games, +-0.7 pt of ATS% is within one standard error.")


if __name__ == "__main__":
    main()
