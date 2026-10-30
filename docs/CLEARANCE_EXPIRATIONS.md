# Manual clearance renewal

Expired is calculated when reading current credentials. Reading the dashboard,
queue, or mobile profile never rejects a document. There is no scheduled job.
The document's saved review status remains Verified until an official acts.

## Officer workflow

The Verification Queue includes an Expired applicant pill and the expiry date on
each affected clearance. The Process expired clearances button previews the number
of current documents and distinct workers. Its confirmation explicitly identifies
the jurisdiction and explains that valid companion documents stay unchanged.

An Admin processes all barangays, regardless of the current table filters. A
Barangay official processes only active kasambahays in their assigned barangay.
Normalized barangay names are matched exactly; street/city substring matches do
not grant access. The server determines the jurisdiction, not the client.

## API

- `GET /api/v1/verifications/admin/expired-clearances/` returns `scope`, `as_of`,
  `document_count`, `worker_count`, and `confirmation_token`. This is read-only.
- POST the same endpoint with `{ "confirmation_token": "..." }` to apply the
  preview. The signed confirmation is officer- and scope-bound and expires after
  five minutes. Only this field is accepted.
- Expiry means `valid_until < today's Philippine date`; a clearance remains
  valid through its expiry day. Missing expiry dates are not invented.
- Only the latest submission of each NBI/police clearance type is considered.
  Already rejected records, valid companions, homeowners, and inactive workers
  are excluded from the action.
- Changed documents, membership, or the Philippine date invalidate the preview
  (409). The officer must preview and confirm again. Tokens cannot be transferred
  between accounts. Concurrent/repeated requests cannot duplicate decisions or
  notifications.
- Each affected document becomes Rejected, with its expiry date and renewal
  instructions in the reason. The officer, audit metadata, and in-app notification
  are saved in the same transaction; failure rolls back all decisions.

The queue provides additive `applicantStatus`, `isExpired`, and `validUntil`
fields. The dashboard summary includes an `expired` count. Both use the same
latest-required-document status helper and active resident scope. Applicant
priority is Rejected, Expired, Verified, Pending, then No Documents. Document
review status and calculated applicant eligibility are separate.

## Renewal and job permissions

The existing mobile rejected-document flow permits immediate re-upload after
processing. Only that document resets to Pending, clears old OCR/reviewer/expiry
metadata, and gets new analysis. Its valid companion remains approved. Job
posting and engagement checks use current credentials, so an expired worker is
ineligible even before an official processes the expiry. Renewal is not approval:
the worker remains ineligible until the latest required documents are approved
and none has a saved past expiry.

Mobile profile status keeps its existing enum: an expired applicant is Unverified
until manual rejection, then Rejected, then Pending during renewal review.

No new schema or migration is needed for expiration handling. The separate,
previously implemented OCR processing lease migration remains required for that
upload code. Neither code deploys itself nor changes production data.

## Limits and validation

Expiry uses saved dates, not an automatic government-site lookup. An incorrect
OCR year such as 2028 instead of 2026 still requires human date verification;
this feature does not silently correct old OCR data.

Tests cover Philippine midnight, same-day validity, permissions, exact barangay
scope, preview tampering and changes, latest-submission rules, valid companions,
audit/notification rollback, real competing PostgreSQL connections, count parity,
mobile renewal upload, and posting/engagement eligibility. Admin tests cover
cancel/confirm/zero/error flows, expiry details and backend-provided status.

Validated locally on October 30, 2026:

- 93 backend tests passed on an isolated PostgreSQL 18 test cluster, including
  the existing OCR/upload, workforce, dashboard, and booking guard regressions.
- 106 admin tests passed, including a local mocked Vite API proxy test.
- Admin TypeScript/Vite build, 50 mobile verification tests, mobile TypeScript,
  Ruff F/E9 checks, and Git whitespace checks passed.
- Migration detection found no additional schema changes for this feature.

Test logs are under the workspace `output/expiry-*.log`. Provider calls were
mocked. No production or restored-backup database was modified, no app deployed,
and no APK built. Actual phone/UI workflow testing remains a local acceptance
check after restarting the backend and refreshing the development apps.
