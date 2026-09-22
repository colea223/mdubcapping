"""
Run this BEFORE starting local work (editing code, running a pipeline
script, pulling) any time you haven't just pulled -- it exists specifically
to kill the git friction Cole hit repeatedly the week this was added:
weekly_pipeline.yml and odds_pull.yml both commit fresh data (db/,
data/raw/, data/clean/, docs/data/, excel/MW_Handicapping_Tracker.xlsx) on
their own schedule, completely independent of anything happening locally.
If you run a pipeline script locally (or a prior local run just left
uncommitted changes sitting around) without pulling first, THOSE local
changes block `git pull` outright -- "Your local changes ... would be
overwritten by merge" -- even when it's not a real conflict, just git
refusing to silently clobber uncommitted work. Same friction either way:
you're stuck until you commit or discard those files by hand.

None of the files this script resets are ever hand-edited -- they're 100%
machine-generated pipeline output (the DB, raw/clean data snapshots, the
site's JSON, the Excel tracker). So there's nothing to lose by discarding
your local copies of JUST those and taking whatever's on origin/main -- if
you want fresher data than that, re-run the relevant pipeline script
AFTER this finishes, then commit/push your own regeneration as usual.

This does NOT touch any of your own code changes (.py files, workflow
files, etc.) -- only the exact path list every workflow's own "Commit
updated data back to the repo" step commits. A real conflict on a .py file
you edited is left alone deliberately -- that's a genuine conflict to
resolve by hand, never something to paper over automatically.

Usage:
    source .venv/bin/activate      (or .venv\\Scripts\\activate on Windows)
    python src/sync_data.py
"""
import subprocess
import sys
import time

# Kept in sync BY HAND with weekly_pipeline.yml's/odds_pull.yml's own
# `git add db/ data/raw/ data/clean/ docs/data/ excel/MW_Handicapping_Tracker.xlsx`
# line -- if a future workflow change adds/removes a path there, update it
# here too.
GENERATED_PATHS = [
    "db/",
    "data/raw/",
    "data/clean/",
    "docs/data/",
    "excel/MW_Handicapping_Tracker.xlsx",
]


def run(cmd):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def main():
    print("Discarding local changes to auto-generated data/tracker files "
          "(never hand-edited, safe to reset)...")
    rc = run(["git", "restore", "--staged", "--worktree", *GENERATED_PATHS])
    if rc != 0:
        print(
            "\ngit restore failed (see above) -- most likely one of the paths above "
            "isn't tracked in this checkout, or this isn't being run from the repo "
            "root. Fix that and re-run rather than pushing past this silently."
        )
        sys.exit(1)

    print("\nPulling latest from origin...")
    rc = run(["git", "pull"])
    if rc != 0:
        print(
            "\ngit pull failed (see above) -- if that's a real conflict on a .py file "
            "you edited, resolve it by hand as usual; this script only ever clears "
            "out the generated-data paths above, nothing else."
        )
        sys.exit(1)

    print(
        "\nDone -- your local data/tracker files now match whatever automation (or "
        "your own last push) last produced. Your own code changes, if any, were left "
        "untouched. Want fresher data than that? Re-run the relevant pipeline script "
        "now, then commit/push your own regeneration as usual."
    )


if __name__ == "__main__":
    _script_start_time = time.time()
    print(f"[Started at {time.strftime('%Y-%m-%d %H:%M:%S')}]")
    main()

    _script_elapsed = time.time() - _script_start_time
    _mins, _secs = divmod(_script_elapsed, 60)
    print(f"\n[Finished in {int(_mins)}m {_secs:04.1f}s]" if _mins else f"\n[Finished in {_secs:.1f}s]")
