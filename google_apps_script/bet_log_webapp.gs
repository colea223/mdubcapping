/**
 * Mountain Dub Handicapping -- Bet Log web app.
 *
 * WHAT THIS IS: docs/log-bet.html is a page on the (fully static, no server)
 * GitHub Pages site. A static site can't write to a file on your PC, so this
 * script is the bridge -- a tiny Google Apps Script Web App, bound to a
 * Google Sheet you own, that the site's form talks to. excel/import_bet_log.py
 * (back in the main repo) then pulls whatever's landed on the Sheet into the
 * tracker's Bet Log tab. See that script's own docstring for the other half.
 *
 * WHY THIS OVER JUST HANDING OUT AN API KEY (e.g. Airtable's): the site is
 * PUBLIC -- anything embedded in its client-side JS is visible to any
 * visitor. A write-capable API key embedded there would let a stranger read
 * or corrupt your data. This script instead deploys as "execute as me" --
 * the PUBLIC URL below can only ever run the functions you write here: submit
 * a bet IF the caller supplies the right WRITE_PASSWORD, or return every
 * submission IF the caller supplies the right READ_TOKEN (a separate secret).
 * Nothing about your Google account, or direct access to the Sheet, is ever
 * exposed, and neither secret is ever committed to the (public) repo or
 * visible in the site's page source: WRITE_PASSWORD lives only here -- you
 * type it into docs/log-bet.html's own password field each time (or once
 * per device, since that page remembers it locally in your browser, never
 * sent anywhere but here); READ_TOKEN lives only here and in your local,
 * gitignored .env.
 *
 * WHY EVERYTHING GOES THROUGH doGet, INCLUDING SUBMITTING A BET (no doPost):
 * a plain cross-origin fetch() from docs/log-bet.html to a doPost here hits a
 * real CORS wall -- Apps Script Web App responses aren't reliably readable by
 * a browser fetch from another origin, so the page could send data but never
 * find out whether it was accepted (this bit us: it silently "succeeded" with
 * a wrong password because the read failed). Loading the request as a
 * <script src="..."> tag (JSONP) sidesteps that entirely -- a <script> tag
 * isn't subject to CORS the way fetch/XHR are, and Apps Script can wrap its
 * JSON response in a JS function call the page defines ahead of time. That
 * only works for GET (a <script> tag can't POST), which is why a bet
 * submission here is a GET with everything -- including the password -- as
 * URL query parameters instead of a POST body. See docs/log-bet.html's own
 * jsonp() helper for the client side of this.
 *
 * ---------------------------------------------------------------- SETUP ---
 * 1. Create a new Google Sheet (sheets.new). Rename its first tab to
 *    "Submissions" (must match SHEET_NAME below).
 * 2. In that Sheet: Extensions > Apps Script. Delete the placeholder code
 *    and paste this whole file in.
 * 3. Pick your own random string for READ_TOKEN below (anything -- e.g.
 *    mash the keyboard) and replace REPLACE_WITH_YOUR_OWN_RANDOM_STRING.
 *    Also pick a WRITE_PASSWORD below -- this is the password the Log Bet
 *    page on the site will ask for before it'll submit anything; make it
 *    something you can actually remember/type on your phone, unlike
 *    READ_TOKEN (which you never type by hand). Save the project (any name
 *    is fine, e.g. "Bet Log Web App").
 * 4. Deploy > New deployment > gear icon > "Web app".
 *      - Execute as: Me
 *      - Who has access: Anyone
 *    Click Deploy, authorize it (it's your own script, on your own account
 *    -- the "Google hasn't verified this app" warning is expected for a
 *    personal script; click Advanced > Go to <project name> (unsafe) to
 *    proceed).
 * 5. Copy the Web app URL it gives you (ends in /exec). Paste it into
 *    docs/log-bet.html as WEBAPP_URL (replacing the PASTE_YOUR_... text).
 * 6. Put the SAME URL and the SAME token you picked in step 3 into your
 *    local .env as BET_WEBAPP_URL / BET_WEBAPP_TOKEN (see .env.example) --
 *    that's what excel/import_bet_log.py reads to pull submissions.
 * 7. Whenever you edit this script later (rare), you have to update THIS
 *    SAME deployment -- Deploy > Manage deployments > pencil icon on the one
 *    whose URL you're actually using > New version > Deploy. Using "New
 *    deployment" instead creates a totally different URL and leaves the old
 *    one (running the old code) still live at the old URL.
 * ----------------------------------------------------------------------- */
const SHEET_NAME = "Submissions";
const READ_TOKEN = "REPLACE_WITH_YOUR_OWN_RANDOM_STRING";
const WRITE_PASSWORD = "REPLACE_WITH_YOUR_OWN_SUBMIT_PASSWORD";

function _sheet() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
}

// callback present -> JSONP (wrap the JSON in a call to that function name,
// served as JavaScript so a <script> tag can execute it). callback absent
// -> plain JSON (what excel/import_bet_log.py's plain HTTP GET gets, same as
// before -- it isn't a browser, so it was never subject to CORS in the first
// place and doesn't need the JSONP wrapper).
function _respond(obj, callback) {
  if (callback) {
    return ContentService.createTextOutput(callback + "(" + JSON.stringify(obj) + ")")
      .setMimeType(ContentService.MimeType.JAVASCRIPT);
  }
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// Everything comes through GET now (see the module docstring for why) --
// e.parameter.action tells doGet which of the two things this request wants.
function doGet(e) {
  const p = e.parameter;
  if (p.action === "submit") {
    return _respond(_submitBet(p), p.callback);
  }
  return _respond(_listSubmissions(p), p.callback);
}

// Called by docs/log-bet.html on every "Log Bet" submit (action=submit).
// Gated by WRITE_PASSWORD -- unlike a plain public contact form, this checks
// a password before appending anything, so knowing this URL alone (visible
// in the site's public page source) isn't enough to submit a bet. Worst case
// of a leaked/guessed WRITE_PASSWORD is junk rows you can just delete, never
// exposure of your actual data (this function never reads the sheet back).
function _submitBet(p) {
  if (!p.password || p.password !== WRITE_PASSWORD) {
    return { ok: false, error: "Wrong password" };
  }
  const required = ["date", "week", "matchup", "bet_type", "side", "line", "odds", "stake"];
  for (const key of required) {
    if (p[key] === undefined || p[key] === null || p[key] === "") {
      return { ok: false, error: "Missing field: " + key };
    }
  }
  try {
    const sheet = _sheet();
    // Self-healing: if the sheet is completely empty (e.g. freshly created,
    // nothing appended yet), write a header row first so _listSubmissions's
    // values.slice(1) below has a real header to skip instead of eating the
    // first real submission -- this bit a fresh sheet that skipped straight
    // to a submission with no header row ever added.
    if (sheet.getLastRow() === 0) {
      sheet.appendRow([
        "id", "submitted_at", "date", "week", "matchup",
        "bet_type", "side", "line", "odds", "stake",
      ]);
    }
    const id = Utilities.getUuid();
    const submittedAt = new Date().toISOString();
    sheet.appendRow([
      id, submittedAt, p.date, p.week, p.matchup,
      p.bet_type, p.side, p.line, p.odds, p.stake,
    ]);
    return { ok: true, id: id };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

// Called by excel/import_bet_log.py (never by the public site, and never
// with a callback param -- that's what keeps this branch a plain HTTP GET
// instead of JSONP) to pull every submission recorded so far. Gated by
// READ_TOKEN -- this is the one operation that can read your data, so it's
// the one that needs the shared secret. A request with a missing/wrong token
// gets nothing back.
function _listSubmissions(p) {
  if (!p.token || p.token !== READ_TOKEN) {
    return { ok: false, error: "Invalid or missing token" };
  }
  try {
    const sheet = _sheet();
    const values = sheet.getDataRange().getValues();
    const rows = values.slice(1).map(function (r) {
      return {
        id: r[0], submitted_at: r[1], date: r[2], week: r[3], matchup: r[4],
        bet_type: r[5], side: r[6], line: r[7], odds: r[8], stake: r[9],
      };
    });
    return { ok: true, rows: rows };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}
