"""
Shared helpers for writing and reading the raw CFBD/Odds API JSON snapshots
that live in data/raw/. Two independent storage-efficiency improvements,
both requested explicitly as a "tighten the files on the database unless it
impacts the models" pass -- neither one changes a single byte of what
ultimately loads into game_features or reaches model.py/xgboost_model.py.
Only how the raw pull scripts write bytes to disk, and how build_db.py reads
them back, changed.

1. COMPRESSION. Every pull_*.py script used to write raw JSON via
   json.dumps(obj, indent=2) -- pretty-printed (2-3x larger than compact
   JSON for no functional reason) and uncompressed. write_json_gz() below
   writes compact JSON (no indent, tight separators) through gzip instead --
   gzip alone gets another 5-10x on top of that for this kind of repetitive,
   mostly-numeric data. New files get a ".json.gz" extension. Nothing here
   touches or rewrites the plain ".json" files already on disk (including
   the real ~2 million plays rows from the full-history backfill) -- those
   keep loading exactly as before, see load_json_any() below.

2. PRUNING. Most raw snapshot types are "latest wins" -- build_db.py's
   latest_snapshots() already only ever reads the newest file per
   (prefix, year), so every older snapshot of the same (prefix, year) just
   sits on disk forever doing nothing but bloating the repo (data/raw/ is
   committed by the GitHub Actions workflows). prune_superseded() deletes
   those older files right after a new one is written successfully.

   NOT every snapshot type is safe to prune this way. pull_lines.py and
   pull_odds_api.py deliberately keep EVERY historical timestamped snapshot
   for the current season -- build_db.py's all_lines_snapshots() and
   build_odds_api_snapshots_table() scan ALL of them (not just the latest)
   to power the website's Line History chart. Those two scripts call
   write_json_gz() and simply never call prune_superseded() -- pruning is
   opt-in, per call site, never automatic, specifically so this can't
   accidentally happen to those two.

BACKWARD COMPATIBILITY. build_db.py's load_json() reads through
load_json_any() here, which transparently handles either extension, and
every glob/regex pattern in build_db.py that scans data/raw/ has been
widened to match both "*.json" and "*.json.gz" -- so years of existing
plain-JSON history keep loading unchanged, and this is purely additive for
new writes going forward.
"""
import gzip
import json
from pathlib import Path


def write_json_gz(path: Path, obj) -> Path:
    """
    Write `obj` as gzip-compressed, compact JSON. `path` should be the
    "normal" .json path a script would otherwise have written to (e.g.
    RAW_DIR / f"drives_{year}_{stamp}.json") -- this writes to that same
    name with ".gz" appended instead, so all the naming/stamping logic
    already in each pull_*.py script is untouched. Returns the actual path
    written, so callers can pass it straight to prune_superseded() or print
    it.
    """
    gz_path = Path(str(path) + ".gz")
    payload = json.dumps(obj, default=str, separators=(",", ":")).encode("utf-8")
    with gzip.open(gz_path, "wb") as f:
        f.write(payload)
    return gz_path


def load_json_any(path: Path):
    """
    Reads a raw snapshot regardless of whether it's gzip-compressed
    (*.json.gz, the new format) or plain text (*.json, every file pulled
    before this change) -- callers don't need to know or care which.
    """
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return json.loads(path.read_text())


def prune_superseded(raw_dir: Path, glob_pattern: str, keep_path: Path) -> list:
    """
    Deletes every file in `raw_dir` matching `glob_pattern` (a glob string,
    e.g. "drives_2026_*.json*" -- the trailing "json*" matches both .json
    and .json.gz) EXCEPT `keep_path` itself. Returns the list of filenames
    removed (for a print statement at the call site).

    Only call this for snapshot types where build_db.py only ever reads the
    latest file per key -- see the module docstring's "latest wins" list.
    NEVER call this from pull_lines.py or pull_odds_api.py.
    """
    keep_resolved = keep_path.resolve()
    removed = []
    for f in raw_dir.glob(glob_pattern):
        if f.resolve() != keep_resolved:
            f.unlink()
            removed.append(f.name)
    return removed
