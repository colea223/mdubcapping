"""Turns off scheduled runs in both GitHub Actions workflows (keeps manual 'Run workflow').
Safe to run more than once. Run from the project root."""
import re
from pathlib import Path

for name in ("odds_pull.yml", "weekly_pipeline.yml"):
    p = Path(".github/workflows") / name
    lines = p.read_text(encoding="utf-8").splitlines()
    out, i, removed = [], 0, False
    while i < len(lines):
        line = lines[i]
        if re.match(r"^\s{2}schedule\s*:\s*$", line):          # "  schedule:" under on:
            removed = True
            i += 1
            # skip its body: deeper-indented lines, comments and blank lines, until the next 2-space key
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("   ") or lines[i].lstrip().startswith("#")):
                if re.match(r"^\s{2}\S", lines[i]) and not lines[i].lstrip().startswith("#"):
                    break
                i += 1
            out.append("  # Scheduled runs are OFF -- run manually from the Actions tab (Run workflow) or locally.")
            continue
        out.append(line)
        i += 1
    p.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    has_cron = any(re.match(r"^\s*-?\s*cron\s*:", l) for l in out)
    print(f"{name}: {'schedule removed' if removed else 'no schedule block found (already off?)'}; cron lines left: {has_cron}")
