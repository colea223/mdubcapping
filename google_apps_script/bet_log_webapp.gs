/**
 * Mountain Dub Handicapping -- Bet Log web app.
 *
 * WHAT THIS IS: docs/log-bet.html is a page on the (fully static, no server)
 * GitHub Pages site. A static site can't write to a file on your PC, so this
 * script is the bridge -- a tiny Google Apps Script Web App, bound to a
 * Google Sheet you own, that the site's form POSTs to. excel/import_bet_log.py
 * (back in the main repo) then pulls whatever's landed on the Sheet into the
 * tracker's Bet Log tab. See that script's own docstring for the other half.
 *
 * WHY THIS OVER JUST HANDING OUT AN API KEY (e.g. Airtable's): the site is
 * PUBLIC -- anything embedded in its client-side JS is visible to any
 * visitor. A write-capable API key embedded there would let a stranger read
 * or corrupt your data. This script instead deploys as "execute as me" --
 * the PUBLIC URL below can only ever run the two functions you write (append
 * a row / return rows IF the caller has the right token), never anything
 * broader. Nothing about your Google account, or direct access to the Sheet,
 * is ever exposed.
 *
 * ---------------------------------------------------------------- SETUP ---
 * 1. Create a new Google Sheet (sheets.new). Rename its first tab to
 *    "Submissions" (must match SHEET_NAME below).
 * 2. In that Sheet: Extensions > Apps Script. Delete the placeholder code
 *    and paste this whole file in.
 * 3. Pick your own random string for READ_TOKEN below (anything -- e.g.
 *    mash the keyboard) and replace REPLACE_WITH_YOUR_OWN_RANDOM_STRING.
 *    Save the project (any name is fine, e.g. "Bet Log Web App").
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
 * 7. Whenever you edit this script later (rare), you have to
 *    Deploy > Manage deployments > edit (pencil) > New version > Deploy for
 *    the change to actually take effect -- saving alone isn't enough.
 * ----------------------------------------------------------------------- */
const SHEET_NAME = "Submissions";
const READ_TOKEN = "REPLACE_WITH_YOUR_OWN_RANDOM_STRING";

function _sheet() {
  return SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

// Called by docs/log-bet.html on every "Log Bet" submit. Deliberately has NO
// token check -- same trust model as any public contact form (anyone can
// submit a row; nobody can READ the sheet through this endpoint). Worst case
// is junk rows you can just delete; there's no way to expose or corrupt
// anything else through this function.
function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    const required = ["date", "week", "matchup", "bet_type", "side", "line", "odds", "stake"];
    for (const key of required) {
      if (body[key] === undefined || body[key] === null || body[key] === "") {
        return _json({ ok: false, error: "Missing field: " + key });
      }
    }
    const id = Utilities.getUuid();
    const submittedAt = new Date().toISOString();
    _sheet().appendRow([
      id, submittedAt, body.date, body.week, body.matchup,
      body.bet_type, body.side, body.line, body.odds, body.stake,
    ]);
    return _json({ ok: true, id: id });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  }
}

// Called by excel/import_bet_log.py (never by the public site) to pull every
// submission recorded so far. Gated by READ_TOKEN -- this is the one
// operation that can read your data, so it's the one that needs the shared
// secret. A request with a missing/wrong token gets nothing back.
function doGet(e) {
  if (!e.parameter.token || e.parameter.token !== READ_TOKEN) {
    return _json({ ok: false, error: "Invalid or missing token" });
  }
  const sheet = _sheet();
  const values = sheet.getDataRange().getValues();
  const rows = values.slice(1).map(function (r) {
    return {
      id: r[0], submitted_at: r[1], date: r[2], week: r[3], matchup: r[4],
      bet_type: r[5], side: r[6], line: r[7], odds: r[8], stake: r[9],
    };
  });
  return _json({ ok: true, rows: rows });
}
