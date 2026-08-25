"""
Runs the full pipeline in order: pull_games -> pull_stats -> pull_lines -> build_db.
Cross-platform (Windows/Mac/Linux) since it's plain Python, not a shell script.

Usage:
    (activate your venv first, then)
    python src/run_pipeline.py
"""
import sys
import time
from pathlib import Path

import pull_games
import pull_stats
import pull_lines
import pull_venues
import build_db
import power_rating
import features
import predict_week
import export_site_data

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "excel"))
import update_tracker  # noqa: E402

STEPS = [
    ("Games", pull_games.main),
    ("Stats (PPA/SP+/Elo/Recruiting)", pull_stats.main),
    ("Lines", pull_lines.main),
    ("Venues", pull_venues.main),
    ("Build DB", build_db.main),
    ("Baseline power ratings", power_rating.main),
    ("Pre-game features", features.main),
    ("Predict next upcoming week", predict_week.main),
    ("Update Excel tracker", update_tracker.main),
    ("Update website data (docs/data/*.json)", export_site_data.main),
]

# Not included above -- run these on their own when you want them, not on
# every refresh:
#   python src/backtest.py                    (an evaluation report, not a data step)
#   python src/predict_week.py --season .. --week ..   (for a week other than the next upcoming one)


def main():
    start = time.time()
    for label, fn in STEPS:
        print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
        try:
            fn()
        except Exception as e:
            print(f"\nPipeline stopped during '{label}': {e}")
            sys.exit(1)
    print(f"\nAll steps done in {time.time() - start:.1f}s.")


if __name__ == "__main__":
    main()
