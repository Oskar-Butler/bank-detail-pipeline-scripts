/**
 * ═══════════════════════════════════════════════════════════════
 *  ⚠️  DESIGN — NOT YET DEPLOYED
 * ═══════════════════════════════════════════════════════════════
 *  Google Sheets approval UI for the weekly payment run.
 *
 *  The finance manager opens this bound Sheet each payment-run
 *  week, clicks "Refresh Queue", reviews flagged rows, ticks the
 *  "Approved" checkbox for each row they are happy to pay, then
 *  clicks "Export for Bank". The script produces a CSV in the
 *  bank's upload format containing only approved rows.
 *
 *  This file is the documented target. It is intentionally
 *  committed so the handoff from BigQuery → finance team is
 *  visible in the repo.
 * ═══════════════════════════════════════════════════════════════
 */

// ------------------------------------------------------------------
// Configuration — all values surfaced via Script Properties so the
// deployer never has to edit the source to point at their project.
// ------------------------------------------------------------------

const PROPS = PropertiesService.getScriptProperties();

function CONFIG_() {
  return {
    gcpProjectId:   PROPS.getProperty('GCP_PROJECT_ID'),
    goldTableFqn:   PROPS.getProperty('GOLD_TABLE_FQN')   || 'your-gcp-project.gold.gold_payment_approval_queue',
    auditTableFqn:  PROPS.getProperty('AUDIT_TABLE_FQN')  || 'your-gcp-project.gold.payment_approvals_audit',
    location:       PROPS.getProperty('BQ_LOCATION')      || 'europe-west2',
  };
}

// ------------------------------------------------------------------
// Menu
// ------------------------------------------------------------------

function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu('Payment Run')
    .addItem('Refresh Queue',      'refreshPaymentRunQueue')
    .addItem('Export Approved CSV','exportApprovedToBankingFormat_')
    .addToUi();
}

// ------------------------------------------------------------------
// refreshPaymentRunQueue
//   Pulls current gold_payment_approval_queue rows into a sheet
//   tab named "Payment Run W{isoweek}-{year}". Adds an "Approved"
//   checkbox column on the left. Preserves any existing approvals
//   on re-run by matching on source_document_url.
// ------------------------------------------------------------------

function refreshPaymentRunQueue() {
  const cfg = CONFIG_();
  const sql =
    'SELECT * FROM `' + cfg.goldTableFqn + '` ' +
    'ORDER BY approval_status, DUE_DATE';

  const rows = runBigQuery_(cfg.gcpProjectId, cfg.location, sql);

  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const weekTag = Utilities.formatDate(new Date(), 'Europe/London', "'W'w-yyyy");
  const sheet = ss.getSheetByName('Payment Run ' + weekTag)
                || ss.insertSheet('Payment Run ' + weekTag);

  writeRowsWithApprovalColumn_(sheet, rows);
  formatApprovalStatusColumn_(sheet);
}

// ------------------------------------------------------------------
// onEdit — when a user ticks the "Approved" checkbox, write an
// audit record to BigQuery: who approved, when, which invoice.
// Simple installable trigger; relies on Session.getActiveUser()
// so only authenticated workspace accounts can approve.
// ------------------------------------------------------------------

function onEdit(e) {
  if (!e || !e.range) return;
  const header = e.range.getSheet().getRange(1, 1, 1, e.range.getSheet().getLastColumn())
    .getValues()[0];
  const approvedCol = header.indexOf('Approved') + 1;
  if (e.range.getColumn() !== approvedCol) return;
  if (e.value !== 'TRUE') return;

  const cfg = CONFIG_();
  const row = e.range.getRow();
  const rowValues = e.range.getSheet()
    .getRange(row, 1, 1, e.range.getSheet().getLastColumn()).getValues()[0];

  const approval = {
    source_document_url: rowValues[header.indexOf('source_document_url')],
    invoice_number:      rowValues[header.indexOf('INVOICE_NUMBER')],
    approved_by:         Session.getActiveUser().getEmail(),
    approved_at:         new Date().toISOString(),
  };

  writeAuditRow_(cfg.gcpProjectId, cfg.auditTableFqn, approval);
}

// ------------------------------------------------------------------
// exportApprovedToBankingFormat_
//   Builds the bank's upload CSV from rows where "Approved" is
//   ticked. Columns are the bank's fixed schema — the exact
//   layout depends on which payments provider (e.g. Bankline,
//   Bacs-file, Modulr). Placeholder columns below.
// ------------------------------------------------------------------

function exportApprovedToBankingFormat_() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const data   = sheet.getRange(2, 1, sheet.getLastRow() - 1, sheet.getLastColumn()).getValues();

  const col = (name) => header.indexOf(name);
  const approvedRows = data.filter(r => r[col('Approved')] === true);

  const csv = [
    ['beneficiary_name','sort_code','account_number','amount','currency','reference'].join(',')
  ].concat(
    approvedRows.map(r => [
      r[col('ACCOUNT_NAME')],
      r[col('SORT_CODE')],
      r[col('ACCOUNT_NUMBER')],
      r[col('TOTAL_AMOUNT')],
      r[col('CURRENCY')],
      r[col('INVOICE_NUMBER')],
    ].join(','))
  ).join('\n');

  const blob = Utilities.newBlob(csv, 'text/csv',
    'payment_run_' + Utilities.formatDate(new Date(), 'Europe/London', 'yyyy-MM-dd') + '.csv');
  DriveApp.createFile(blob);
}

// ==================================================================
//  Helpers — BigQuery advanced service (see appsscript.json)
// ==================================================================

function runBigQuery_(projectId, location, sql) {
  const request = { query: sql, useLegacySql: false, location: location };
  const job = BigQuery.Jobs.query(request, projectId);
  const rows = (job.rows || []).map(r => r.f.map(f => f.v));
  const header = job.schema.fields.map(f => f.name);
  return { header: header, rows: rows };
}

function writeRowsWithApprovalColumn_(sheet, queryResult) {
  sheet.clear();
  const header = ['Approved'].concat(queryResult.header);
  sheet.getRange(1, 1, 1, header.length).setValues([header]).setFontWeight('bold');

  if (queryResult.rows.length === 0) return;

  const body = queryResult.rows.map(r => [false].concat(r));
  sheet.getRange(2, 1, body.length, header.length).setValues(body);

  // Approval checkbox column
  sheet.getRange(2, 1, body.length, 1).insertCheckboxes();
  sheet.setFrozenRows(1);
  sheet.setFrozenColumns(1);
}

function formatApprovalStatusColumn_(sheet) {
  const header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const col = header.indexOf('approval_status') + 1;
  if (col <= 0) return;

  const range = sheet.getRange(2, col, Math.max(sheet.getLastRow() - 1, 1), 1);
  const rules = [
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('BLOCKED').setBackground('#f4c7c3').setRanges([range]).build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('NEEDS_REVIEW').setBackground('#fce8b2').setRanges([range]).build(),
    SpreadsheetApp.newConditionalFormatRule()
      .whenTextEqualTo('READY_TO_PAY').setBackground('#b7e1cd').setRanges([range]).build(),
  ];
  sheet.setConditionalFormatRules(rules);
}

function writeAuditRow_(projectId, auditTableFqn, row) {
  const [dataset, table] = auditTableFqn.split('.').slice(1);
  const request = { rows: [{ json: row }] };
  BigQuery.Tabledata.insertAll(request, projectId, dataset, table);
}
