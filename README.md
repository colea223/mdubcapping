# Mountain Dub Handicapping — Project Scaffold

This is the Phase 0/1 build from the attack plan: a working data pipeline skeleton
plus the Excel tracker. Everything runs; the only thing missing is your free CFBD
API key, since that's not something I can sign up for on your behalf.

## What's here

```
mw-handicapping/
  Mountain_West_Handicapping_Attack_Plan.docx   the original plan doc
  requirements.txt                              pinned Python dependencies
  .env.example                                  copy to .env and add your CFBD key
  src/
    config.py            paths, year range, API key loading
    teams.py              2026 MW team crosswalk + name-alias normalization
    cfbd_client.py        thin wrapper around the cfbd API client
    pull_games.py          pulls every FBS game 2016-2026 + North Dakota State's FCS history
    pull_stats.py           pulls advanced/PPA stats, SP+, Elo, recruiting composite
    pull_lines.py           pulls betting lines (run 2x/week in-season: opening + closing)
    build_db.py             loads all raw pulls into db/mw_handicapping.duckdb
  db/
    schema.sql             DuckDB table definitions
    mw_handicapping.duckdb  the database itself (currently just the team reference table)
  data/raw/                 immutable timestamped JSON snapshots land here
  data/clean/               reserved for feature-engineered output (Phase 2, not built yet)
  excel/
    build_tracker.py                  regenerates the tracker from scratch
    MW_Handicapping_Tracker.xlsx       Read Me / Settings / Weekly Slate / Bet Log / Team Profiles
  notebooks/                reserved for EDA once real data is flowing
```

## One-time setup

1. Get a free API key: https://collegefootballdata.com/key (takes about a minute).
2. Copy `.env.example` to `.env` and paste your key into `CFBD_API_KEY=`.
3. Create the virtual environment and install dependencies.

   **Mac/Linux:**
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   **Windows (Command Prompt):** use `python`, not `python3` — Windows doesn't
   have a `python3` command even when Python is installed (if you see it try to
   open the Microsoft Store instead of running, that's the tell). If your prompt
   shows `(base)`, you already have a real Python on PATH via Anaconda.
   ```
   python -m venv .venv
   .venv\Scripts\activate.bat
   pip install -r requirements.txt
   ```

   **Windows (PowerShell):**
   ```
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   If PowerShell blocks the activate script with an execution-policy error, run
   this once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## Running the pipeline

Activate the virtual environment first (see the command for your OS above), then:
```
python src/pull_games.py     # every FBS game 2016-2026, plus NDSU's FCS-era games
python src/pull_stats.py     # advanced/PPA stats, SP+, Elo, recruiting
python src/pull_lines.py     # betting lines (re-run weekly once the season starts)
python src/build_db.py       # loads everything above into db/mw_handicapping.duckdb
```

Each pull script writes a new timestamped file to `data/raw/` rather than overwriting —
`build_db.py` always loads the newest snapshot per year. This matters most for lines:
run `pull_lines.py` once when a week's lines open and again right before kickoff, and
you'll have both snapshots to compute closing line value (CLV) later.

## The Excel tracker

`excel/MW_Handicapping_Tracker.xlsx` is ready to use today, independent of the Python
side — its Team Profiles tab is already pre-filled with all 10 current Mountain West
teams and their conference history. Open the Read Me tab first; blue cells are yours
to fill in, black cells are formulas, yellow cells on the Settings tab are the
assumptions (starting bankroll, edge thresholds) everything else references.

## What's not built yet (next session)

- **Feature engineering** (Phase 2): turning the raw game/stat tables into pre-game,
  leakage-free model features (opponent-adjusted efficiency, rest days, travel,
  altitude delta).
- **Baseline power rating + regression model** (Phases 2-3): the actual spread/total
  projections.
- **Walk-forward backtest harness** (Phase 3): CLV, ROI, calibration reporting.
- **Historical closing-line backfill**: CFBD's own line history has gaps in older
  seasons; supplementing with an external archive (e.g. Sports Book Review Online)
  is still to do.

Everything above is scaffolded and tested (with synthetic data standing in for a real
API pull) — the moment your CFBD key is in `.env`, the whole chain from `pull_games.py`
through `build_db.py` runs against real data with no code changes needed.

## The website (docs/)

`docs/` is a plain static site — four pages (`index.html`, `matchups.html`,
`predictions.html`, `tracking.html`) that read JSON files from `docs/data/`. Nothing on
the page itself talks to Python or DuckDB; `src/export_site_data.py` is the one script
that turns your database into those JSON files (`python src/run_pipeline.py` now runs it
automatically as the last step). Open any of the four `.html` files directly in a
browser to preview locally — no server needed.

## Deploying the site for free (GitHub Pages + GitHub Actions)

This gets you a real URL (`https://<your-username>.github.io/<repo-name>/`) that updates
itself on a schedule, with nothing running on your own computer. It's a one-time setup;
after that, GitHub's own free servers pull fresh data and refresh the site for you.

**1. Create a GitHub account and a new repository.**
   - Sign up at https://github.com/signup if you don't already have an account.
   - Click the **+** in the top-right corner → **New repository**.
   - Name it something like `mw-handicapping` (any name works — it becomes part of your
     site's URL). Leave it **Public** (GitHub Pages' free tier requires a public repo,
     unless you're on a paid plan). Don't check "Add a README" — you already have one.
   - Click **Create repository**. Keep the page open — it shows the commands from step 2.

**2. Push this project to that repository.**
   Open a terminal in this project folder (the one with `README.md`, `src/`, `docs/`,
   etc.) and run, substituting your own GitHub username and the repo name you chose:
   ```
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```
   If `git` prompts you to log in, follow its instructions (it may open a browser window,
   or ask for a personal access token instead of your password — GitHub will guide you).

**3. Add your CFBD API key as a repository secret.**
   The scheduled runs need your key, but it should never be committed into the repo in
   plain text — a "secret" is GitHub's encrypted, write-only stand-in for that.
   - On your repo's GitHub page, go to **Settings → Secrets and variables → Actions**.
   - Click **New repository secret**.
   - Name: `CFBD_API_KEY`. Value: paste your actual key (the same one in your local `.env`).
   - Click **Add secret**.

**4. Turn on GitHub Pages.**
   - Still in **Settings**, click **Pages** in the left sidebar.
   - Under **Build and deployment → Source**, choose **Deploy from a branch**.
   - Under **Branch**, choose `main` and `/docs`, then **Save**.
   - GitHub will show your site's URL at the top of that page once it's live (usually
     within a minute or two). That's the link to send anyone.

**5. Run the pipeline once so there's real data to show.**
   The workflow at `.github/workflows/weekly_pipeline.yml` is already set up to run
   automatically twice a week (Sunday and Wednesday mornings) — but you don't have to
   wait for the schedule:
   - Go to the **Actions** tab on your repo.
   - Click **Update Mountain Dub Handicapping data** in the left sidebar.
   - Click **Run workflow** (dropdown on the right) → **Run workflow** again to confirm.
   - It takes a few minutes. When it finishes with a green check, refresh your GitHub
     Pages URL from step 4 — the rankings, matchups, predictions, and tracking pages
     should all be populated.

From then on, the site updates itself on the built-in schedule with zero action from
you. If you ever want an extra refresh right after a game finishes, use the same
**Run workflow** button from step 5. To change how often it runs, edit the `cron:` lines
near the top of `.github/workflows/weekly_pipeline.yml` — each line is
`minute hour day month weekday` in UTC.

Injury notes and other qualitative info on the Predictions page aren't something CFBD
provides automatically — edit `site_notes.json` in the project root (format:
`"Away Team @ Home Team": "note text"`) and commit/push the change; the next pipeline
run will pick it up.
