# Finance Approval UI (Google Sheets)

> ⚠️ **DESIGN — not yet deployed.** This folder documents the intended approval surface for the payment-run pipeline. The Dataform gold view ([../dataform/definitions/gold/gold_payment_approval_queue.sqlx](../dataform/definitions/gold/gold_payment_approval_queue.sqlx)) produces the data this UI consumes.

## Why this exists

The finance manager runs payments weekly. Before this pipeline, that meant opening every supplier invoice one by one, copying bank details into the bank's upload template, cross-checking job numbers and amounts, and watching for duplicates — several hours per run.

The upstream OCR pipeline already extracts all the structured fields. This sheet is the human-in-the-loop layer: it shows the pre-triaged queue, lets the manager tick what they approve, and exports the bank's upload CSV for only those approved rows.

## What the sheet looks like

```
┌──────────┬──────────────────┬───────────────┬───────────┬──────────────────┬────────────────────┬─────────────────────────────┐
│ Approved │ approval_status  │ SUPPLIER_NAME │ INVOICE # │ TOTAL_AMOUNT GBP │ SORT / ACC / IBAN  │ review_reasons              │
├──────────┼──────────────────┼───────────────┼───────────┼──────────────────┼────────────────────┼─────────────────────────────┤
│   ☐      │ READY_TO_PAY     │ Acme Foods    │ INV-2041  │           4,250  │ 200000 / 12345678  │ []                          │
│   ☐      │ READY_TO_PAY     │ Globex Retail │ GLX-0912  │          11,800  │ 400530 / 87654321  │ []                          │
│   ☐      │ NEEDS_REVIEW     │ Initech       │ 2024-INT7 │           2,600  │         (missing)  │ [Missing account number]    │
│   ☐      │ BLOCKED          │ Acme Foods    │ INV-2041  │           4,250  │ 200000 / 12345678  │ [Duplicate invoice 90d]     │
└──────────┴──────────────────┴───────────────┴───────────┴──────────────────┴────────────────────┴─────────────────────────────┘
```

Rows are coloured by `approval_status`:
- 🟢 **READY_TO_PAY** — every rule passes, safe to batch-approve
- 🟡 **NEEDS_REVIEW** — one or more soft flags, manager eyes required
- 🔴 **BLOCKED** — hard failure (no bank details, total mismatch, duplicate), cannot be approved

## How it works

1. Manager opens the bound Sheet, clicks **Payment Run → Refresh Queue**.
2. `refreshPaymentRunQueue()` queries the gold view and writes rows into a tab named `Payment Run W{week}-{year}`.
3. Manager reviews `NEEDS_REVIEW` rows, clicking the `source_document_url` to open the original invoice in Drive.
4. Manager ticks the `Approved` checkbox on rows they are happy to pay. Each tick fires the `onEdit` trigger, which writes an audit row to `payment_approvals_audit` in BigQuery (who approved, when, which invoice) for a full paper trail.
5. Manager clicks **Payment Run → Export Approved CSV**, which drops a bank-format CSV into Drive containing only approved rows.

## Files

- [Code.gs](Code.gs) — all script logic (stubbed, documented).
- [appsscript.json](appsscript.json) — manifest: enables the BigQuery advanced service + OAuth scopes.

## Configuration

Values are read from Script Properties — no secrets in source:

| Property | Description |
|---|---|
| `GCP_PROJECT_ID` | Project hosting the gold view and audit table |
| `GOLD_TABLE_FQN` | e.g. `your-gcp-project.gold.gold_payment_approval_queue` |
| `AUDIT_TABLE_FQN` | e.g. `your-gcp-project.gold.payment_approvals_audit` |
| `BQ_LOCATION` | e.g. `europe-west2` |

## What's still to do before this ships

- Build out the bank-upload CSV schema (placeholder columns in [Code.gs](Code.gs) — the exact layout depends on which payments provider).
- Create the `payment_approvals_audit` BigQuery table (DDL not yet committed).
- Add a "Pull updated gold view" trigger on a timer so the sheet refreshes overnight before each payment run.
- Wire a Slack notifier that pings the manager when `BLOCKED` count changes.
