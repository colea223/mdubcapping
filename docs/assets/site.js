// One High Coverage (formerly Mountain Dub Handicapping) -- shared helpers used by every page.

const NAV_LINKS = [
  { href: "index.html", label: "Home" },
  { href: "ratings.html", label: "Ratings" },
  { href: "matchups.html", label: "Matchups" },
  { href: "lines.html", label: "Live Lines" },
  { href: "predictions.html", label: "Predictions" },
  { href: "results.html", label: "Results" },
  { href: "tracking.html", label: "Tracking" },
  { href: "matchup.html", label: "Matchup Creator" },
  { href: "log-bet.html", label: "Log Bet" },
  { href: "about-model.html", label: "About the Model" },
];

// Every FBS conference this site covers -- mirrors teams.py's own
// FBS_CONFERENCES allowlist exactly (kept in sync by hand since this file
// has no access to the Python source; see that file's own comment for why
// a conference-name allowlist, not a team roster, is the stable boundary).
// "All Conferences" is a synthetic option, not a real CFBD conference name.
const FBS_CONFERENCES = [
  "ACC", "American Athletic", "Big 12", "Big Ten", "Conference USA",
  "FBS Independents", "Mid-American", "Mountain West", "Pac-12", "SEC", "Sun Belt",
];
const ALL_CONFERENCES = "All Conferences";

// Real URL slug per conference, e.g. "Mountain West" -> "mountain-west" --
// used to build the static per-conference page filenames
// (ratings-mountain-west.html, matchups-mountain-west.html, ...) that
// generate_pages.py actually writes to disk. Cole was explicit that he
// wants each conference view to be its own real, bookmarkable page, not a
// dropdown/query-string filter -- this replaces the old ?conf= convention
// everywhere on the site.
const CONFERENCE_SLUGS = {
  "ACC": "acc", "American Athletic": "american-athletic", "Big 12": "big-12",
  "Big Ten": "big-ten", "Conference USA": "conference-usa",
  "FBS Independents": "fbs-independents", "Mid-American": "mid-american",
  "Mountain West": "mountain-west", "Pac-12": "pac-12", "SEC": "sec", "Sun Belt": "sun-belt",
};

function confSlug(conference) {
  return CONFERENCE_SLUGS[conference] || conference.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

// Which conference THIS page is scoped to. generate_pages.py stamps
// `window.FIXED_CONFERENCE = "Mountain West";` (etc.) into every
// <kind>-<slug>.html file it writes, before site.js loads; the un-suffixed
// page (ratings.html, matchups.html, ...) leaves it null, meaning "All FBS".
function getPageConference() {
  return (typeof window.FIXED_CONFERENCE !== "undefined" && window.FIXED_CONFERENCE) || ALL_CONFERENCES;
}

// Plain link strip across the top of every Ratings/Matchups/Predictions/
// Live Lines page -- "All FBS" plus one real link per conference, styled
// like tabs -- replacing the old <select> dropdown entirely. `kind` is the
// page family ("ratings" | "matchups" | "predictions" | "lines"); `active`
// is this page's own conference (ALL_CONFERENCES for the un-suffixed page).
function renderConferenceLinkStrip(kind, active) {
  const allClass = active === ALL_CONFERENCES ? "conf-link active" : "conf-link";
  const links = [`<a class="${allClass}" href="${kind}.html">All FBS</a>`];
  FBS_CONFERENCES.forEach(c => {
    const cls = c === active ? "conf-link active" : "conf-link";
    links.push(`<a class="${cls}" href="${kind}-${confSlug(c)}.html">${c}</a>`);
  });
  return `<div class="conf-link-strip">${links.join("")}</div>`;
}

// True if a game/team's conference matches the current filter -- "All
// Conferences" always matches; otherwise either side of a matchup (home or
// away) matching is enough, same "this game involves that conference" rule
// the Excel per-conference tabs use.
function matchesConference(selected, ...conferences) {
  if (selected === ALL_CONFERENCES) return true;
  return conferences.includes(selected);
}

function renderNav(activeHref) {
  const links = NAV_LINKS.map(l =>
    `<a href="${l.href}" class="${l.href === activeHref ? "active" : ""}">${l.label}</a>`
  ).join("");
  return `
    <nav class="site-nav">
      <div class="site-nav-inner">
        <div class="site-brand">One <span>High</span> Coverage</div>
        <div class="site-nav-links">${links}</div>
      </div>
    </nav>`;
}

async function fetchJSON(path) {
  try {
    const res = await fetch(path, { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null;
  }
}

// homeSpread: negative = home favored (this project's convention throughout).
// Returns { homeLabel, awayLabel } e.g. "-3.5" / "+3.5".
function formatSpreadPair(homeSpread) {
  if (homeSpread === null || homeSpread === undefined) return { home: "TBD", away: "TBD" };
  const h = homeSpread;
  const a = -homeSpread;
  const fmt = (v) => (v > 0 ? `+${v}` : `${v}`);
  return { home: fmt(h), away: fmt(a) };
}

function formatMoneyline(v) {
  if (v === null || v === undefined) return "--";
  return v > 0 ? `+${v}` : `${v}`;
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T12:00:00Z");
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

// Full timestamp (date pulled straight from CFBD, already UTC) rendered in
// Eastern time -- matches how odds sites label kickoff times.
function formatTimeET(iso) {
  if (!iso) return "TBD";
  const d = new Date(iso);
  const parts = d.toLocaleString("en-US", {
    timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
  return `${parts} ET`;
}

// Kickoff time only (no date), Eastern -- e.g. "8:00 PM EDT". Used in each
// card's meta row once a day-group header (see groupByDay() below) is
// already showing the date, so the card itself doesn't need to repeat it.
function formatKickoffTime(iso) {
  if (!iso) return "TBD";
  const d = new Date(iso);
  const time = d.toLocaleTimeString("en-US", {
    timeZone: "America/New_York", hour: "numeric", minute: "2-digit",
  });
  const tz = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", timeZoneName: "short",
  }).formatToParts(d).find(p => p.type === "timeZoneName").value;
  return `${time} ${tz}`;
}

// "THU, OCT 1" -- big day-group header label, Eastern calendar day (a game
// at 11:59 PM ET Saturday and one at 12:30 AM ET Sunday are on different
// real-world days for anyone reading the slate, so this groups by the same
// Eastern date formatTimeET()/formatKickoffTime() already display, not UTC).
function dayHeaderLabel(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric",
  }).toUpperCase();
}

// Groups an already date-sorted array into [{ label, items }] buckets by
// Eastern calendar day (see dayHeaderLabel()) -- the "THU, OCT 1 -- 2 games"
// header treatment used on Matchups/Predictions, styled after a reference
// layout Cole liked. Assumes `items` is already sorted by `dateField`
// (every caller's source JSON already is), so this only needs to detect
// when the label changes, never re-sort.
function groupByDay(items, dateField) {
  const groups = [];
  let current = null;
  for (const item of items) {
    const label = dayHeaderLabel(item[dateField]);
    if (!current || current.label !== label) {
      current = { label, items: [] };
      groups.push(current);
    }
    current.items.push(item);
  }
  return groups;
}

// Movement arrow + colored delta for a line that has moved from its open.
// Icon + color together (never color alone) per the site's accessibility rule.
function movementTag(current, open) {
  if (current === null || current === undefined) return "";
  if (open === null || open === undefined || open === current) return "";
  const delta = current - open;
  const arrow = delta > 0 ? "&#9650;" : "&#9660;";
  const cls = delta > 0 ? "move-up" : "move-down";
  return ` <span class="move-tag ${cls}">${arrow} ${Math.abs(delta).toFixed(1)}</span>`;
}

function lastUpdatedLabel(generatedAt) {
  if (!generatedAt) return "";
  const d = new Date(generatedAt);
  return `Last updated ${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} at ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}

// Derives a projected final score from the model's own spread + total --
// there's no separate "projected score" model output, so this just solves
// the two-equation system every odds site uses for the same purpose:
// margin = homeScore - awayScore, total = homeScore + awayScore. Rounded to
// whole points for display (a score is never fractional); the +/-0.5 from
// rounding both sides independently is an acceptable display-only fudge,
// same tradeoff any site showing a "projected score" next to a half-point
// spread makes.
function projectedScore(modelSpreadHome, modelTotal) {
  if (modelSpreadHome === null || modelSpreadHome === undefined
    || modelTotal === null || modelTotal === undefined) return null;
  const margin = -modelSpreadHome; // homeSpread negative = home favored = positive home margin
  const home = Math.round((modelTotal + margin) / 2);
  const away = Math.round((modelTotal - margin) / 2);
  return { home, away };
}

function emptyState(message) {
  return `<div class="empty-state">${message}</div>`;
}

// {team: logo_url} built from rankings.json's own `logo` field (CFBD's own
// hosted team logos -- see src/pull_team_logos.py/export_site_data.py's
// team_logo_map()). The Ratings page gets `logo` on every row for free
// already (build_rankings() puts it there directly); every other page
// (Home, Matchups, Predictions, Live Lines, Results) has no per-team logo
// field of its own, so it fetches data/rankings.json purely as a lookup
// table and builds this map client-side -- the exact same "team -> extra
// info" pattern index.html's own RANK_MAP already established for ranks.
// Missing entirely (fetch failed, or rankings.json not generated yet) just
// yields an empty map, not an error -- every caller already treats "no
// logo for this team" as a normal, silent no-op (see teamLogoImg() below).
function teamLogoMap(rankingsData) {
  if (!rankingsData || !rankingsData.rankings) return {};
  return Object.fromEntries(
    rankingsData.rankings.filter(r => r.logo).map(r => [r.team, r.logo])
  );
}

// <img> tag for one team's logo, or "" if this team has none on file yet
// (a new team logos hasn't been pulled for, an FCS buy-game opponent that
// never appears in rankings.json at all, etc.) -- always safe to splice
// into a template literal, never leaves a broken <img> in the markup.
// `extraClass` layers on a size/position variant (e.g. the marquee card's
// bigger centered logo) on top of the shared `.team-logo` base rule.
function teamLogoImg(team, logoMap, extraClass = "") {
  const src = logoMap && logoMap[team];
  if (!src) return "";
  const cls = extraClass ? `team-logo ${extraClass}` : "team-logo";
  return `<img class="${cls}" src="${src}" alt="" loading="lazy">`;
}
