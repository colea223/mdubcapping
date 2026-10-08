"""Turns off scheduled runs in both GitHub Actions workflows (keeps manual 'Run workflow')."""
from pathlib import Path

for name in ("odds_pull.yml", "weekly_pipeline.yml"):
    p = Path(".github/workflows") / name
    s = p.read_text(encoding="utf-8")
    i = s.index("on:\n  schedule:")
    j = s.index("  workflow_dispatch: {}")
    s = (s[:i] + "on:\n  # Scheduled runs are OFF -- run manually from the Actions tab (Run workflow) or locally.\n" + s[j:])
    p.write_text(s, encoding="utf-8", newline="\n")
    print("updated", p)
