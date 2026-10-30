# Reviewer corrections in Document Preview

Reviewers can open **Edit details** in a submitted document's preview, correct
confirmed mistakes, and save without leaving the page. Saving is separate from
approval/rejection and never changes the document's review decision or reviewer.

## Editable fields

- Account: first, middle and last name; birthdate.
- Document: first, middle and last name; birthdate; document/clearance number;
  clearance issue date and expiry date.
- PhilSys National IDs do not offer issue/expiry dates.

Username, email, password, contact number, barangay, role, account access, document
type, source image, raw OCR text, and review decision are not editable through
this API. Account and document corrections are independent; no field is silently
copied from one to the other.

## Backend

`GET /api/v1/verifications/admin/documents/<document_id>/corrections/` returns the
editable values and a version. PATCH the same endpoint with `version` and changed
`profile` and/or `document` fields. Unknown fields are rejected. The version
prevents stale forms, duplicate saves, and edits meant for a replaced image.
Changed data requires closing and reopening the form (409).

Only active Admin/Barangay reviewers may access the API. Admins can correct
residents across all barangays. Barangay reviewers are restricted to exact,
normalized matches with their assigned barangay. Current required submissions
are editable; obsolete submissions cannot replace their successors.

Dates must be valid; birthdates and issue dates cannot be in the future. When
clearance dates are edited, expiry cannot precede issue date. Clearing optional
fields is allowed. A correction does not invent missing dates or approve a user.

The service locks the document records and resident, applies changes and refreshes
mismatch flags/scores. If the account changes, matching is refreshed for its
documents too. Dates and document numbers remain consistent with the displayed
structured document data and existing eligibility/history rules.

Human document corrections use the existing `extracted_data` JSON under
`_reviewer_corrections`; no new table/column is added. Raw OCR text stays intact.
An in-flight OCR attempt is invalidated when a correction supersedes its values.
Explicit reanalysis preserves corrected fields; renewal uploads clear them with
the old image's analysis. Provider output cannot mark itself as human-reviewed.

## Existing Audit Trail

Each changed save appends one `UPDATED` event to `tbl_audit_logs`, with the
existing actor, target resident, document, timestamp and request telemetry.
`metadata.reason_code = reviewer_correction` and `metadata.changes` record each
field's section, name, old value and new value. No-op saves create no event.
If audit creation fails, the entire correction rolls back.

The existing Audit Trail displays the changes and includes them in search and
its existing report export. Audit access now requires a signed-in reviewer;
barangay reviewers cannot retrieve another barangay's identity corrections by
altering query parameters.

## Validation

106 backend tests passed on isolated PostgreSQL, including corrections, real
competing edits, audit rollback, expiry, OCR/upload, dashboard and booking guards.
118 admin tests passed, including inline save/cancel/error flows, locked fields,
audit display and existing portal regressions. The admin TypeScript/Vite build
and Ruff F/E9 checks passed.

No migration is required. Tests mock external providers and do not modify Neon
or the restored application database. Nothing was deployed and no APK was built.
Test logs are in the workspace `output/corrections-*.log`.
