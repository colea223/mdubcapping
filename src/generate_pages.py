"""
Generates a real, separate static .html page per FBS conference for each of
Ratings/Matchups/Predictions/Live Lines, from the four base templates
already in docs/ (ratings.html, matchups.html, predictions.html, lines.html)
-- Cole was explicit he wants each conference view to be its own
bookmarkable page, not a client-side dropdown/query-string filter (see
site.js's renderConferenceLinkStrip()/getPageConference(), which every
generated page relies on to know which conference it is and to link to its
siblings).

Each base template contains exactly one line:
    <script>window.FIXED_CONFERENCE = null;</script>
This script does a plain text substitution of that ONE line (nothing else in
the file is touched) to produce docs/<kind>-<slug>.html for every conference,
and prefixes the <title> tag so a browser tab/bookmark shows which
conference it is.

Safe to re-run any time -- every generated file is fully rebuilt from its
base template on each run, never hand-edited in place, so there's nothing to
lose by regenerating. Runs as its own run_pipeline.py step, after
export_site_data.py (conceptually the last "refresh the site" step, even
though it doesn't itself read rankings.json/matchups.json -- the base
templates' own JS does that client-side same as always).

Usage:
    source .venv/bin/activate
    python src/generate_pages.py
"""
import re
from pathlib import Path

from teams import FBS_CONFERENCES

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

KINDS = ["ratings", "matchups", "predictions", "lines"]

FIXED_CONF_MARKER = '<script>window.FIXED_CONFERENCE = null;</script>'
FIXED_CONF_RE = re.compile(re.escape(FIXED_CONF_MARKER))

# Kept in sync BY HAND with site.js's own CONFERENCE_SLUGS -- see that
# file's comment. Two independent copies (Python here, JS there) rather
# than one shared source, same tradeoff site.js's FBS_CONFERENCES list
# already makes with teams.py.
CONFERENCE_SLUGS = {
    "ACC": "acc", "American Athletic": "american-athletic", "Big 12": "big-12",
    "Big Ten": "big-ten", "Conference USA": "conference-usa",
    "FBS Independents": "fbs-independents", "Mid-American": "mid-american",
    "Mountain West": "mountain-west", "Pac-12": "pac-12", "SEC": "sec", "Sun Belt": "sun-belt",
}


def conf_slug(conference):
    return CONFERENCE_SLUGS.get(conference, conference.lower().replace(" ", "-"))


def generate_for_kind(kind):
    base_path = DOCS / f"{kind}.html"
    if not base_path.exists():
        print(f"  [generate_pages] {base_path.name} not found -- skipping {kind}")
        return
    base_html = base_path.read_text(encoding="utf-8")
    if not FIXED_CONF_RE.search(base_html):
        print(f"  [generate_pages] WARNING: {base_path.name} has no FIXED_CONFERENCE marker -- "
              f"per-conference {kind} pages NOT regenerated (was the base template edited?)")
        return

    written = 0
    for conference in sorted(FBS_CONFERENCES):
        slug = conf_slug(conference)
        out_html = FIXED_CONF_RE.sub(
            f'<script>window.FIXED_CONFERENCE = "{conference}";</script>', base_html
        )
        out_html = out_html.replace("<title>", f"<title>{conference} ", 1)
        (DOCS / f"{kind}-{slug}.html").write_text(out_html, encoding="utf-8")
        written += 1
    print(f"  [generate_pages] {kind}: wrote {written} conference page(s)")


def main():
    for kind in KINDS:
        generate_for_kind(kind)


if __name__ == "__main__":
    main()
