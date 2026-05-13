/**
 * Combined Apps Script for the CRM spreadsheet.
 *
 * Adds two menus:
 *   - "🔃 Sort" on the People tab (existing — Priority / Outreach / Stage presets)
 *   - "Followups" on any "<Operator> - Followups" tab (new — within-section
 *     sort: Action needed / Days since)
 *
 * Install:
 *   1. Extensions → Apps Script
 *   2. Replace Code.gs contents with this file. Save.
 *   3. Reload the spreadsheet. Both menus appear next to Help.
 *   4. First click on a menu item triggers an auth prompt — approve.
 *
 * Schema dependency (Followups section):
 *   Column order must match FU_HEADERS in linkedin/notifications/sheets.py.
 *   If you reorder columns there, update FU_COL below.
 */

// ============================================================================
// People tab sort (existing)
// ============================================================================

const SHEET_NAME = 'People';

// Highest progression first — these match the dropdown order set by
// /tmp/add_dropdowns.py. If you tweak the dropdown ordering there, mirror
// the changes here.
const PRIORITY_RANK = {
  'High':   3,
  'Medium': 2,
  'Low':    1,
};

const OUTREACH_RANK = {
  'Won':                  12,
  'Prospecting to close': 11,
  'Had Meeting':          10,
  'Meeting Booked':        9,
  'Wants Meeting':         8,
  'Replied':               7,
  'Waiting':               6,
  'Connected':             5,
  'Invite Sent':           4,
  'Manual followup':       3,
  "Don't send":            2,
  'Lost':                  1,
};

const STAGE_RANK = {
  'Won':           6,
  'Closing':       5,
  'Meeting':       4,
  'Qualification': 3,
  'Prospecting':   2,
  'Lost':          1,
};


// Both onOpen and onSelectionChange call rebuildMenus_ so the visible
// menus track the active tab. onOpen handles initial load; onSelectionChange
// fires whenever the user clicks any cell — including the implicit
// selection move that happens when switching tabs — so the menu set updates
// as you move between People and Followups tabs.
function onOpen() {
  rebuildMenus_();
}

function onSelectionChange(e) {
  rebuildMenus_();
}

function rebuildMenus_() {
  const ui = SpreadsheetApp.getUi();
  const sheetName = SpreadsheetApp.getActiveSheet().getName();

  // 🔃 Sort — only on the People tab. Re-adding a menu with the same name
  // replaces the previous instance, so off-tab the menu is created with a
  // single inert "(not applicable)" item that makes it clear why nothing
  // shows up. (Apps Script has no API to fully remove a custom menu.)
  if (sheetName === SHEET_NAME) {
    ui.createMenu('🔃 Sort')
      .addItem('By Priority',                    'sortByPriority')
      .addItem('By Outreach status',             'sortByOutreach')
      .addItem('By Priority + Outreach status',  'sortByPriorityAndOutreach')
      .addSeparator()
      .addItem('By Priority + Stage (legacy)',   'sortByPriorityAndStage')
      .addToUi();
  } else {
    ui.createMenu('🔃 Sort')
      .addItem('(switch to People tab)', 'sortNotApplicableHint_')
      .addToUi();
  }

  // Followups — only on a "<Operator> - Followups" tab.
  if (sheetName.endsWith(' - Followups')) {
    ui.createMenu('Followups')
      .addItem('Sort: Action needed (flat — sent → bottom)', 'sortActionNeededFlat')
      .addItem('Sort: Days since (oldest first)',            'sortDaysSince')
      .addSeparator()
      .addItem('Sort: Action needed (within sections)',      'sortActionNeeded')
      .addItem('Restore section view (un-hide dividers)',    'fuRestoreSectionView_')
      .addSeparator()
      .addItem('Debug: inspect row by name',                 'fuDebugInspectRow_')
      .addItem('Debug: dump section breakdown',              'fuDebugDumpSections_')
      .addToUi();
  } else {
    ui.createMenu('Followups')
      .addItem('(switch to a Followups tab)', 'followupsNotApplicableHint_')
      .addToUi();
  }
}

function sortNotApplicableHint_() {
  SpreadsheetApp.getActive().toast(
    'Switch to the "' + SHEET_NAME + '" tab to use these sorts.'
  );
}

function followupsNotApplicableHint_() {
  SpreadsheetApp.getActive().toast(
    'Switch to a "<Operator> - Followups" tab to use these sorts.'
  );
}

function sortByPriority() {
  sortPeople_([{col: 'Priority', rank: PRIORITY_RANK}]);
}

function sortByOutreach() {
  sortPeople_([{col: 'Outreach status', rank: OUTREACH_RANK}]);
}

function sortByPriorityAndOutreach() {
  sortPeople_([
    {col: 'Priority',        rank: PRIORITY_RANK},
    {col: 'Outreach status', rank: OUTREACH_RANK},
  ]);
}

function sortByPriorityAndStage() {
  sortPeople_([
    {col: 'Priority', rank: PRIORITY_RANK},
    {col: 'Stage',    rank: STAGE_RANK},
  ]);
}

/**
 * Core sort routine. `specs` is a list of {col, rank} pairs, applied left-to-right
 * (first spec is the primary sort key). Each rank is a {value: number} map; higher
 * number = sorts higher. Unknown values fall to the bottom of their bucket.
 */
function sortPeople_(specs) {
  const ss = SpreadsheetApp.getActive();
  const sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    SpreadsheetApp.getUi().alert(`No tab named "${SHEET_NAME}".`);
    return;
  }

  const range = sheet.getDataRange();
  const all = range.getValues();
  if (all.length < 2) return;  // header only

  const headers = all[0];
  const data = all.slice(1);

  // Validate spec columns exist in headers.
  for (const s of specs) {
    if (headers.indexOf(s.col) === -1) {
      SpreadsheetApp.getUi().alert(`Column "${s.col}" not found.`);
      return;
    }
  }
  const indexedSpecs = specs.map(s => ({
    idx: headers.indexOf(s.col),
    rank: s.rank,
  }));

  data.sort((a, b) => {
    for (const s of indexedSpecs) {
      const ra = s.rank[a[s.idx]] || 0;
      const rb = s.rank[b[s.idx]] || 0;
      if (ra !== rb) return rb - ra;  // descending: high rank first
    }
    return 0;
  });

  // Write back. setValues replaces the data range while preserving formatting
  // (cell colors, conditional formats, etc).
  sheet.getRange(2, 1, data.length, headers.length).setValues(data);
  ss.toast(`Sorted ${data.length} rows by ${specs.map(s => s.col).join(' + ')}.`);
}


// ============================================================================
// Followups tab sort (new — namespaced with FU_ prefix)
// ============================================================================

const FU_NUM_COLS = 15;

const FU_COL = {
  NAME: 1,
  STATUS: 2,
  COHORT: 3,
  ROLE: 4,
  PRIORITY: 5,
  DAYS_SINCE: 6,
  DAYS_SINCE_CONN: 7,
  CONVO: 8,
  DRAFT_EMAIL: 9,
  EMAIL_LINK: 10,
  SENT_EMAIL: 11,
  DRAFT_LI: 12,
  LI_URL: 13,
  SENT_LI: 14,
  QUALIFY: 15,
};

// Followups PRIORITY vocabulary differs from People tab (5 tiers vs 3),
// so it gets its own rank map — do NOT reuse PRIORITY_RANK above.
const FU_PRIORITY_RANK = {
  'HIGH':        5,
  'MEDIUM-HIGH': 4,
  'MEDIUM':      3,
  'LOW':         2,
  'HOLD':        1,
};

function sortActionNeeded() {
  // 2 tiers, both sorted PRIORITY desc (HIGH first within each tier):
  //   Tier 0 (nothing sent yet): top — these need action.
  //   Tier 1 (anything sent on either channel): bottom — done.
  // Within the sent block, HIGH sits above LOW (priority is just a
  // tiebreaker among already-sent rows). The tier separation is absolute:
  // every sent row sits below every unsent row in the same section.
  fuSortWithinSections_(function (row) {
    const anySent = (fuIsSent_(row[FU_COL.SENT_EMAIL - 1]) ||
                     fuIsSent_(row[FU_COL.SENT_LI - 1])) ? 1 : 0;
    const prio = FU_PRIORITY_RANK[String(row[FU_COL.PRIORITY - 1] || '').trim().toUpperCase()] || 0;
    return [anySent, -prio];
  });
}

// Accepts the dropdown string "Yes", a literal boolean TRUE (in case
// the cell was ever swapped to a checkbox), a "TRUE" string, "y", or
// common manual entries. Anything else (including blank or "No") is
// treated as not-sent.
function fuIsSent_(v) {
  if (v === true) return true;
  if (v === false || v == null) return false;
  const s = String(v).trim().toLowerCase();
  return s === 'yes' || s === 'true' || s === 'y' || s === '✓' || s === 'x';
}

// Divider rows START with one of the known section emojis. The earlier
// "col A filled, col B empty" heuristic was unsafe — any data row with
// a blank Status would get misdetected as a divider, splitting a real
// section into mini-sections that the sort can't move rows across.
function fuIsDivider_(row) {
  const colA = String(row[0] == null ? '' : row[0]).trim();
  if (!colA) return false;
  const prefixes = ['🤝', '💬', '⏳', '🌊', '✅'];
  for (let i = 0; i < prefixes.length; i++) {
    if (colA.indexOf(prefixes[i]) === 0) return true;
  }
  return false;
}

function sortActionNeededFlat() {
  // Flat sort across the entire tab. Section dividers get moved to the
  // bottom and hidden so the visible list is a single ranked stream:
  // 0 sent → top, 1 sent → middle, 2 sent → bottom; within each tier,
  // PRIORITY desc. Cohort grouping is lost in this view — run "Restore
  // section view" or re-run the Python rebuild to bring it back.
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!sheet.getName().endsWith(' - Followups')) {
    SpreadsheetApp.getUi().alert('Switch to a Followups tab.');
    return;
  }
  const lastRow = sheet.getLastRow();
  if (lastRow < 3) return;

  const range = sheet.getRange(2, 1, lastRow - 1, FU_NUM_COLS);
  const values = range.getValues();
  const formulas = range.getFormulas();
  const merged = values.map(function (row, i) {
    return row.map(function (v, j) {
      return formulas[i][j] ? formulas[i][j] : v;
    });
  });

  const dataRows = [];
  const dividerRows = [];
  for (let i = 0; i < merged.length; i++) {
    if (fuIsDivider_(merged[i])) {
      dividerRows.push(merged[i]);
      continue;
    }
    const nm = String(merged[i][FU_COL.NAME - 1] || '').trim();
    if (!nm) continue;
    dataRows.push(merged[i]);
  }

  dataRows.sort(function (a, b) {
    const aSent = (fuIsSent_(a[FU_COL.SENT_EMAIL - 1]) ? 1 : 0) +
                  (fuIsSent_(a[FU_COL.SENT_LI - 1]) ? 1 : 0);
    const bSent = (fuIsSent_(b[FU_COL.SENT_EMAIL - 1]) ? 1 : 0) +
                  (fuIsSent_(b[FU_COL.SENT_LI - 1]) ? 1 : 0);
    if (aSent !== bSent) return aSent - bSent;
    const aPrio = FU_PRIORITY_RANK[String(a[FU_COL.PRIORITY - 1] || '').trim().toUpperCase()] || 0;
    const bPrio = FU_PRIORITY_RANK[String(b[FU_COL.PRIORITY - 1] || '').trim().toUpperCase()] || 0;
    return bPrio - aPrio;
  });

  // Layout: data rows at top, dividers parked at the bottom, blank rows
  // padding in between if the original block had any.
  const out = dataRows.concat(dividerRows);
  while (out.length < merged.length) {
    out.push(new Array(FU_NUM_COLS).fill(''));
  }
  range.setValues(out);

  // Hide the divider rows so the visible list is one continuous stream.
  // They sit at sheet rows (2 + dataRows.length) .. (2 + dataRows.length + dividerRows.length - 1).
  if (dividerRows.length > 0) {
    sheet.hideRows(2 + dataRows.length, dividerRows.length);
  }
  SpreadsheetApp.getActive().toast(
    'Flat sort applied — ' + dataRows.length + ' rows, sent rows at bottom.'
  );
}

function fuRestoreSectionView_() {
  // Un-hide all rows so dividers reappear. Note: rows aren't physically
  // moved back into cohort order — that requires re-running the Python
  // write_followups task (sync_sheets / followup workflow).
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!sheet.getName().endsWith(' - Followups')) {
    SpreadsheetApp.getUi().alert('Switch to a Followups tab.');
    return;
  }
  const lastRow = sheet.getMaxRows();
  sheet.showRows(1, lastRow);
  SpreadsheetApp.getActive().toast(
    'Dividers visible again. Re-run the Python followup task to ' +
    'restore cohort grouping.'
  );
}

function sortDaysSince() {
  fuSortWithinSections_(function (row) {
    const ds = parseInt(row[FU_COL.DAYS_SINCE - 1], 10);
    return [isNaN(ds) ? 0 : -ds];
  });
}

/**
 * Sort rows within each section of the active Followups tab.
 * Reads formulas alongside values so HYPERLINK cells (Email Link,
 * LinkedIn Message Url) survive. Writes each section back as one range
 * so divider merges stay intact.
 */
function fuSortWithinSections_(keyFn) {
  const sheet = SpreadsheetApp.getActiveSheet();
  const name = sheet.getName();
  if (!name.endsWith(' - Followups')) {
    SpreadsheetApp.getUi().alert(
      'Active tab is "' + name + '". Switch to a tab named ' +
      '"<Operator> - Followups" and try again.'
    );
    return;
  }
  const lastRow = sheet.getLastRow();
  if (lastRow < 3) return;

  const range = sheet.getRange(2, 1, lastRow - 1, FU_NUM_COLS);
  const values = range.getValues();
  const formulas = range.getFormulas();

  // Where a cell has a formula, keep the formula string so setValues
  // re-evaluates it. Otherwise keep the raw value.
  const merged = values.map(function (row, i) {
    return row.map(function (v, j) {
      return formulas[i][j] ? formulas[i][j] : v;
    });
  });

  // Walk rows, splitting on section dividers.
  const sections = [];
  let current = null;
  for (let i = 0; i < merged.length; i++) {
    if (fuIsDivider_(merged[i])) {
      current = { slotIdx: [], rows: [] };
      sections.push(current);
    } else if (current) {
      // Skip blank trailing rows; they'd otherwise sort to one end and
      // create empty space at the top of a section.
      const rowName = String(merged[i][FU_COL.NAME - 1] || '').trim();
      if (!rowName) continue;
      current.slotIdx.push(i);
      current.rows.push(merged[i]);
    }
  }

  for (let s = 0; s < sections.length; s++) {
    const section = sections[s];
    if (section.rows.length === 0) continue;
    section.rows.sort(function (a, b) {
      return fuCmpKey_(keyFn(a), keyFn(b));
    });
    const startSheetRow = 2 + section.slotIdx[0];
    sheet.getRange(startSheetRow, 1, section.rows.length, FU_NUM_COLS)
      .setValues(section.rows);
  }
  SpreadsheetApp.getActive().toast('Sorted ' + name + ' within sections.');
}

function fuCmpKey_(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return 0;
}

// ============================================================================
// Diagnostic helpers — surface exactly what the sort sees in the sheet.
// ============================================================================

function fuDebugInspectRow_() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!sheet.getName().endsWith(' - Followups')) {
    SpreadsheetApp.getUi().alert('Switch to a Followups tab.');
    return;
  }
  const ui = SpreadsheetApp.getUi();
  const resp = ui.prompt('Inspect row', 'Enter name (partial, case-insensitive):',
                         ui.ButtonSet.OK_CANCEL);
  if (resp.getSelectedButton() !== ui.Button.OK) return;
  const query = resp.getResponseText().trim().toLowerCase();
  if (!query) return;

  const lastRow = sheet.getLastRow();
  if (lastRow < 3) { ui.alert('Sheet has no data rows.'); return; }
  const values = sheet.getRange(2, 1, lastRow - 1, FU_NUM_COLS).getValues();

  let lastDivider = '(none — row before any section)';
  let lastDividerSheetRow = -1;
  const out = [];
  for (let i = 0; i < values.length; i++) {
    const row = values[i];
    if (fuIsDivider_(row)) {
      lastDivider = String(row[0] || '');
      lastDividerSheetRow = i + 2;
      continue;
    }
    const rowName = String(row[FU_COL.NAME - 1] || '');
    if (rowName.toLowerCase().indexOf(query) === -1) continue;

    const eRaw = row[FU_COL.SENT_EMAIL - 1];
    const liRaw = row[FU_COL.SENT_LI - 1];
    const prioRaw = row[FU_COL.PRIORITY - 1];
    const eSent = fuIsSent_(eRaw);
    const liSent = fuIsSent_(liRaw);
    const sentCount = (eSent ? 1 : 0) + (liSent ? 1 : 0);
    const prio = FU_PRIORITY_RANK[String(prioRaw || '').trim().toUpperCase()] || 0;

    out.push(
      'Name: ' + rowName + '\n' +
      '  Sheet row: ' + (i + 2) + '\n' +
      '  Section: ' + lastDivider + ' (divider at row ' + lastDividerSheetRow + ')\n' +
      '  Sent Email raw: ' + JSON.stringify(eRaw) + ' (' + (typeof eRaw) + ') → isSent=' + eSent + '\n' +
      '  Sent LinkedIn raw: ' + JSON.stringify(liRaw) + ' (' + (typeof liRaw) + ') → isSent=' + liSent + '\n' +
      '  PRIORITY raw: ' + JSON.stringify(prioRaw) + ' → rank=' + prio + '\n' +
      '  Sort key (Action needed): [' + sentCount + ', ' + (-prio) + ']'
    );
  }
  ui.alert(out.length === 0 ? ('No matches for "' + query + '".') :
                              'Matches:\n\n' + out.join('\n\n'));
}

function fuDebugDumpSections_() {
  const sheet = SpreadsheetApp.getActiveSheet();
  if (!sheet.getName().endsWith(' - Followups')) {
    SpreadsheetApp.getUi().alert('Switch to a Followups tab.');
    return;
  }
  const lastRow = sheet.getLastRow();
  if (lastRow < 3) { SpreadsheetApp.getUi().alert('Sheet has no data rows.'); return; }
  const values = sheet.getRange(2, 1, lastRow - 1, FU_NUM_COLS).getValues();

  const sections = [];
  let current = null;
  for (let i = 0; i < values.length; i++) {
    const row = values[i];
    if (fuIsDivider_(row)) {
      current = { label: String(row[0] || ''), startSheetRow: i + 2, rows: [] };
      sections.push(current);
      continue;
    }
    if (!current) continue;
    const nm = String(row[FU_COL.NAME - 1] || '').trim();
    if (!nm) continue;
    const eSent = fuIsSent_(row[FU_COL.SENT_EMAIL - 1]);
    const liSent = fuIsSent_(row[FU_COL.SENT_LI - 1]);
    const sentCount = (eSent ? 1 : 0) + (liSent ? 1 : 0);
    const prio = FU_PRIORITY_RANK[String(row[FU_COL.PRIORITY - 1] || '').trim().toUpperCase()] || 0;
    current.rows.push({ nm: nm.slice(0, 28), sentCount: sentCount, prio: prio });
  }

  let out = 'Sections detected: ' + sections.length + '\n\n';
  if (sections.length === 0) {
    out += '⚠️  No section dividers found. First 5 data rows raw:\n';
    for (let i = 0; i < Math.min(5, values.length); i++) {
      out += '  Row ' + (i + 2) + ': A=' + JSON.stringify(values[i][0]) +
             ' B=' + JSON.stringify(values[i][1]) + '\n';
    }
    SpreadsheetApp.getUi().alert(out);
    return;
  }
  for (let s = 0; s < sections.length; s++) {
    const sec = sections[s];
    out += sec.label + '  (' + sec.rows.length + ' rows, divider row ' + sec.startSheetRow + ')\n';
    for (let r = 0; r < sec.rows.length; r++) {
      const row = sec.rows[r];
      out += '  [' + row.sentCount + ',' + (-row.prio) + '] ' + row.nm + '\n';
    }
    out += '\n';
  }
  SpreadsheetApp.getUi().alert(out);
}
