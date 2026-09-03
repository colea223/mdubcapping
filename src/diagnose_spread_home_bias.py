"""
Follow-up to diagnose_spread_bias.py's headline finding: the model leans
Home more often than the market does (52.2% of all FBS games, 54.4% of MW
games -- 50% would mean no skew), and Home leans lose more than Away leans in
both slices -- dramatically so for MW (46.3% win rate / -11.68% ROI on Home
leans vs. 53.5% / +2.15% on Away leans). That diagnostic only compared the
model to the MARKET (edge = market_spread_home - model_spread_home) though.
This one asks three sharper questions:

  1. Is the home optimism a bias against the market specifically, or against
     ACTUAL GAME OUTCOMES too? residual = pred_margin - actual_margin,
     completely independent of any market line. A model with no systematic
     bias should average close to 0 here (over- and under-predictions of
     home performance should roughly cancel across thousands of games). A
     mean residual that's clearly positive means the model predicts home
     teams to outscore their actual margins on average -- a real property
     of the model itself, not an artifact of comparing it to the market.
  2. Does it concentrate around the Mountain West's three altitude-venue
     home teams -- Air Force, Wyoming, New Mexico -- where
     elevation_delta_away_ft (how much higher the game's venue sits than
     the AWAY team's own home elevation -- see features.py) could plausibly
     be over-crediting the home team's altitude edge? Checked two ways: a
     straight correlation between elevation_delta_away_ft and the residual,
     and a bucketed breakdown (a real effect might only show up past some
     real altitude gap, not as a straight line).
     UPDATE after the first real run: this hypothesis did NOT hold up. The
     altitude-hosting subgroup actually showed a SMALLER residual (+0.365,
     median -0.304) than MW's non-altitude home teams (+1.511), and the
     elevation/residual correlation was ~0 in both slices (+0.0065 all FBS,
     +0.0402 MW) with no consistent trend across elevation buckets. Kept in
     this script for the record and in case future data changes the picture,
     but elevation is not the driver of the MW-wide bias (+1.299 mean
     residual for MW vs +0.190 league-wide) -- see check 3 below instead.
  3. Since it isn't altitude, is it concentrated in a handful of specific MW
     home teams (pointing at a team-specific data/rating issue) or spread
     fairly evenly across the conference (pointing at something structural
     about how the model treats MW as a whole -- e.g. SP+/PPA priors being
     less reliable for a smaller conference, or conference_game_flag
     interacting oddly with a league that plays mostly itself)? Reports
     mean/median residual per MW home team, plus a rough z-score
     (mean / standard-error-of-the-mean) as a sanity check on which teams'
     numbers are far enough from zero to take seriously vs. which are just
     small samples -- NOT a rigorous significance test (these games aren't
     independent draws -- the same team's ratings/injuries/scheme persist
     across its own games within a season), just a way to flag which rows
     deserve more weight.

Reuses run_backtest() directly (same convention as the other diagnose_*.py
scripts), then joins in elevation_delta_away_ft from game_features by
game_id -- backtest.py's own results don't carry it.

Usage:
    source .venv/bin/activate
    python src/diagnose_spread_home_bias.py
"""
import duckdb
import numpy as np
import pandas as pd
import time

from config import DB_PATH
from backtest import run_backtest
from teams import MW_TEAMS_2026

ALTITUDE_HOME_TEAMS = {"Air Force", "Wyoming", "New Mexico"}

ELEV_BINS = [-float("inf"), -500, 0, 500, 1500, float("inf")]
ELEV_LABELS = [
    "<-500ft (away team descending)", "-500 to 0ft", "0 to 500ft",
    "500 to 1500ft", ">1500ft (big altitude jump for away team)",
]


def _residual_stats(df: pd.DataFrame, label: str):
    n = len(df)
    if n == 0:
        print(f"  {label:<32} n=0")
        return
    mean_resid = df["residual"].mean()
    median_resid = df["residual"].median()
    print(f"  {label:<32} n={n:>6}  mean_residual={mean_resid:+.3f} pts  median={median_resid:+.3f} pts")


def main():
    con = duckdb.connect(str(DB_PATH))
    df = run_backtest(con)
    if df.empty:
        print("diagnose: not enough graded games yet -- run the full pipeline first.")
        con.close()
        return

    # pred_margin isn't stored directly in backtest.py's results --
    # model_spread_home = -pred_margin (model.margin_to_model_spread_home),
    # so this just undoes that.
    df["pred_margin"] = -df["model_spread_home"]
    df["residual"] = df["pred_margin"] - df["actual_margin"]  # + = model over-predicted home margin

    elev = con.execute(
        "SELECT game_id, elevation_delta_away_ft FROM game_features"
    ).fetchdf()
    con.close()
    df = df.merge(elev, on="game_id", how="left")

    print("=" * 72)
    print("1. Raw residual vs ACTUAL outcomes (independent of the market line)")
    print("   + = model predicted a bigger home margin than actually happened")
    print("=" * 72)
    _residual_stats(df, "Overall (all FBS)")
    _residual_stats(df[df["is_mw_game"]], "Mountain West-involved")
    print()
    is_altitude_home = df["home_team"].isin(ALTITUDE_HOME_TEAMS)
    _residual_stats(df[is_altitude_home], "MW altitude home teams (AFA/WYO/UNM hosting)")
    _residual_stats(df[df["is_mw_game"] & ~is_altitude_home], "MW non-altitude home teams")
    print()

    print("=" * 72)
    print("2. Does the bias track elevation_delta_away_ft directly?")
    print("=" * 72)
    graded = df.dropna(subset=["elevation_delta_away_ft", "residual"]).copy()
    corr = graded["elevation_delta_away_ft"].corr(graded["residual"])
    print(f"  Correlation, all FBS  (n={len(graded)}): {corr:+.4f}")
    mw_graded = graded[graded["is_mw_game"]].copy()
    corr_mw = mw_graded["elevation_delta_away_ft"].corr(mw_graded["residual"])
    print(f"  Correlation, MW-involved (n={len(mw_graded)}): {corr_mw:+.4f}")
    print("  (positive correlation = residual grows as the away team faces a bigger")
    print("   elevation jump -- i.e. the model over-predicts home margin more the")
    print("   higher the altitude gap, which is exactly the over-crediting pattern")
    print("   the altitude-venue hypothesis predicts)")
    print()

    graded["elev_bucket"] = pd.cut(graded["elevation_delta_away_ft"], bins=ELEV_BINS, labels=ELEV_LABELS)
    print("  Mean residual by elevation_delta_away_ft bucket (all FBS):")
    for label, sl in graded.groupby("elev_bucket", observed=True):
        if len(sl) == 0:
            continue
        print(f"    {label:<42} n={len(sl):>6}  mean_residual={sl['residual'].mean():+.3f} pts")
    print()

    mw_graded["elev_bucket"] = pd.cut(mw_graded["elevation_delta_away_ft"], bins=ELEV_BINS, labels=ELEV_LABELS)
    print("  Mean residual by elevation_delta_away_ft bucket (MW-involved):")
    for label, sl in mw_graded.groupby("elev_bucket", observed=True):
        if len(sl) == 0:
            continue
        print(f"    {label:<42} n={len(sl):>6}  mean_residual={sl['residual'].mean():+.3f} pts")
    print()

    print("=" * 72)
    print("3. Residual by individual MW home team (only games where this team")
    print("   IS the home team -- not just MW-involved)")
    print("=" * 72)
    mw_home = df[df["home_team"].isin(MW_TEAMS_2026)].copy()
    grp = mw_home.groupby("home_team")["residual"]
    team_stats = grp.agg(n="count", mean_residual="mean", median_residual="median", std_residual="std")
    team_stats["se"] = team_stats["std_residual"] / np.sqrt(team_stats["n"])
    team_stats["z"] = team_stats["mean_residual"] / team_stats["se"]
    team_stats = team_stats.sort_values("mean_residual", ascending=False)
    print(f"  {'Team':<20} {'n':>5} {'mean_resid':>11} {'median':>8} {'std':>7} {'rough_z':>8}")
    for team, row in team_stats.iterrows():
        z = f"{row['z']:+.2f}" if pd.notna(row["z"]) else "--"
        print(f"  {team:<20} {int(row['n']):>5} {row['mean_residual']:>+10.3f}  {row['median_residual']:>+7.3f} {row['std_residual']:>7.2f} {z:>8}")
    print()
    print("  |rough_z| roughly above ~2 is worth a closer look; treat this as a")
    print("  flag, not a verdict -- a team's own games aren't independent draws,")
    print("  so this overstates real confidence somewhat.")


if __name__ == "__main__":
    _script_start_time = time.time()
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
