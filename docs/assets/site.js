// Mountain Dub Handicapping -- shared helpers used by every page.

const NAV_LINKS = [
  { href: "index.html", label: "Home" },
  { href: "matchups.html", label: "Matchups" },
  { href: "lines.html", label: "Live Lines" },
  { href: "predictions.html", label: "Predictions" },
  { href: "tracking.html", label: "Tracking" },
];

function renderNav(activeHref) {
  const links = NAV_LINKS.map(l =>
    `<a href="${l.href}" class="${l.href === activeHref ? "active" : ""}">${l.label}</a>`
  ).join("");
  return `
    <nav class="site-nav">
      <div class="site-nav-inner">
        <div class="site-brand">Mountain <span>Dub</span> Handicapping</div>
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

function emptyState(message) {
  return `<div class="empty-state">${message}</div>`;
}
