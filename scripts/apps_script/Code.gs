/**
 * Billboard status endpoint — read and write sign up/down state.
 *
 * Deployed as a web app (execute as the owner, accessible to anyone with the
 * link) and bound to the spreadsheet holding the status tabs. The map page
 * calls it directly from the browser; the deployment URL and the shared token
 * live inside the map's encrypted payload, so only passphrase holders ever see
 * them.
 *
 * This script deliberately handles ONE thing: a mapping from record id to
 * "up" / "down" / unset. Addresses, contact details and coordinates reach the
 * map through the scheduled build instead, and this script never reads the
 * form-response tab even when it is bound to the same spreadsheet.
 *
 * Setup
 *   1. Extensions -> Apps Script, paste this file.
 *   2. Project Settings -> Script Properties -> WRITE_TOKEN = a long random
 *      string. It is NOT stored in this file, which is committed to a public
 *      repository.
 *   3. Create the two tabs below with their header rows.
 *   4. Deploy -> New deployment -> Web app. Copy the /exec URL.
 *
 * Re-deploying after an edit issues a new URL; update STATUS_ENDPOINT then.
 */

var STATUS_TAB = 'status';
var LOG_TAB = 'status_log';

var STATUS_HEADERS = ['id', 'street', 'suburb', 'state', 'updated', 'by', 'note'];
var LOG_HEADERS = ['when', 'id', 'street', 'suburb', 'from', 'to', 'by', 'note'];

// The only values a client may write. A human editing the sheet by hand can
// type anything; readState_ passes that through untouched so the map can show
// it as unrecognised rather than silently discarding someone's edit.
var WRITABLE_STATES = ['up', 'down', ''];

var LOCK_TIMEOUT_MS = 10000;
var MAX_FIELD_CHARS = 500;

/** GET ?token=... -> { ok, v, fetched, states: { id: {state, updated, by, note} } } */
function doGet(e) {
  try {
    var params = (e && e.parameter) || {};
    if (!tokenOk_(params.token)) return json_({ ok: false, error: 'bad token' });
    return json_({
      ok: true,
      v: 1,
      fetched: new Date().toISOString(),
      states: readAll_()
    });
  } catch (err) {
    return json_({ ok: false, error: String(err && err.message || err) });
  }
}

/**
 * POST a JSON body: { token, id, street, suburb, state, by, note }
 *
 * Sent as text/plain rather than application/json on purpose. Apps Script web
 * apps do not answer the CORS preflight that application/json triggers, so the
 * request has to stay a "simple" one: no custom headers, which is also why the
 * token travels in the body instead of an Authorization header.
 */
function doPost(e) {
  try {
    var body;
    try {
      body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    } catch (parseErr) {
      return json_({ ok: false, error: 'body is not JSON' });
    }

    if (!tokenOk_(body.token)) return json_({ ok: false, error: 'bad token' });

    var id = trim_(body.id);
    if (!/^[0-9a-f]{6,64}$/.test(id)) return json_({ ok: false, error: 'bad id' });

    var state = trim_(body.state).toLowerCase();
    if (WRITABLE_STATES.indexOf(state) === -1) {
      return json_({ ok: false, error: 'bad state: ' + state });
    }

    var record = {
      id: id,
      street: trim_(body.street),
      suburb: trim_(body.suburb),
      state: state,
      by: trim_(body.by),
      note: trim_(body.note)
    };

    var lock = LockService.getScriptLock();
    if (!lock.tryLock(LOCK_TIMEOUT_MS)) {
      return json_({ ok: false, error: 'busy, try again' });
    }
    try {
      // Resolve both tabs before touching either. Writing the status row and
      // then failing to log it would leave the sheet holding a change the
      // caller was told had failed - and the map reverts on failure, so the
      // two would silently disagree.
      var statusSheet = sheet_(STATUS_TAB, STATUS_HEADERS);
      var logSheet = sheet_(LOG_TAB, LOG_HEADERS);

      var now = new Date().toISOString();
      var previous = upsert_(statusSheet, record, now);
      appendLog_(logSheet, record, previous, now);
      return json_({
        ok: true,
        id: record.id,
        state: record.state,
        updated: now,
        by: record.by,
        previous: previous
      });
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    return json_({ ok: false, error: String(err && err.message || err) });
  }
}

/* ---------- helpers ---------- */

function json_(payload) {
  return ContentService
    .createTextOutput(JSON.stringify(payload))
    .setMimeType(ContentService.MimeType.JSON);
}

function trim_(value) {
  if (value === null || value === undefined) return '';
  return String(value).trim().slice(0, MAX_FIELD_CHARS);
}

/**
 * Compare against the stored token without leaking its length through timing.
 * Not a high-value target - the token guards a list of sign states - but the
 * comparison costs nothing to do properly.
 */
function tokenOk_(supplied) {
  var expected = PropertiesService.getScriptProperties().getProperty('WRITE_TOKEN');
  if (!expected) throw new Error('WRITE_TOKEN script property is not set');
  if (typeof supplied !== 'string' || supplied.length !== expected.length) return false;
  var diff = 0;
  for (var i = 0; i < expected.length; i++) {
    diff |= supplied.charCodeAt(i) ^ expected.charCodeAt(i);
  }
  return diff === 0;
}

/**
 * Fetch one of the two tabs by name, failing closed if it is absent.
 *
 * Named lookup only, never an index or a scan: bound to the response
 * spreadsheet this script sits alongside a tab full of personal details, and
 * it must not be able to wander into it. It creates nothing either - an
 * absent tab is a setup mistake to report, not to paper over.
 */
function sheet_(name, headers) {
  var sheet = SpreadsheetApp.getActive().getSheetByName(name);
  if (!sheet) {
    throw new Error(
      'No tab named "' + name + '" in this spreadsheet. Create it with the ' +
      'header row: ' + headers.join(', ')
    );
  }
  return sheet;
}

/** Every data row of a tab, or [] when only the header row exists. */
function rows_(sheet, width) {
  var last = sheet.getLastRow();
  if (last < 2) return [];
  return sheet.getRange(2, 1, last - 1, width).getValues();
}

function readAll_() {
  var sheet = sheet_(STATUS_TAB, STATUS_HEADERS);
  var states = {};
  rows_(sheet, STATUS_HEADERS.length).forEach(function (row) {
    var id = trim_(row[0]);
    if (!id) return;
    states[id] = {
      state: trim_(row[3]).toLowerCase(),
      updated: trim_(row[4]),
      by: trim_(row[5]),
      note: trim_(row[6])
    };
  });
  return states;
}

/** Write the row for this id, returning whatever state it held before. */
function upsert_(sheet, record, now) {
  var existing = rows_(sheet, STATUS_HEADERS.length);

  var rowNumber = 0;
  var previous = '';
  for (var i = 0; i < existing.length; i++) {
    if (trim_(existing[i][0]) === record.id) {
      rowNumber = i + 2;              // +1 for the header, +1 for 1-based rows
      previous = trim_(existing[i][3]).toLowerCase();
      break;
    }
  }

  var values = [[
    record.id, record.street, record.suburb,
    record.state, now, record.by, record.note
  ]];

  if (rowNumber) {
    sheet.getRange(rowNumber, 1, 1, STATUS_HEADERS.length).setValues(values);
  } else {
    sheet.appendRow(values[0]);
  }
  return previous;
}

/**
 * Append-only history. Hand edits made directly in the sheet do not pass
 * through here, so this records app writes only.
 */
function appendLog_(sheet, record, previous, now) {
  sheet.appendRow([
    now, record.id, record.street, record.suburb,
    previous, record.state, record.by, record.note
  ]);
}
