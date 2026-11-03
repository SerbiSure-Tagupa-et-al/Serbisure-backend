# Profile photo review

Profile photos now use `tbl_documents.document_type = 'profile_photo'`. The
existing `document_url` column stores the image and syncs to `profile_link`.
No tables, columns, audit fields, or native mobile libraries were added.

## Rules

- Homeowners need approved National ID front, National ID back, and profile photo.
- Kasambahays need approved, valid NBI and Police clearances and an approved photo.
- Missing, pending, or rejected photos prevent posting and booking eligibility.
- Existing residents with nonempty `profile_link` receive an approved photo record
  during the one-time migration. Existing photo reviews are preserved.
- Officials approve or reject a photo separately from the identity documents.
  Rejection requires a reason. Barangay reviewers stay within their own barangay.
- Photo uploads and reviews write to the existing audit trail. A failed audit
  prevents the upload/review database changes from being committed.
- The initial registration selfie becomes pending. After that, only a rejected
  photo can be replaced. Pending and verified photos return 403 on replacement.
- The mobile retake uses the registration liveness camera and quality checks,
  without gallery selection or changed detection thresholds. A retake becomes
  pending, not automatically verified. Photos do not go through document OCR.

## Existing endpoints

- `PUT /api/v1/accounts/profile-image/` — initial selfie or rejected-photo retake,
  multipart `profile_image`; keeps the existing signed `profile_link` response.
- `GET /api/v1/verifications/status/` — includes the photo document, required
  credentials, overall eligibility, and signed `profile_link` for avatar refresh.
- `POST /api/v1/verifications/admin/review/<document_id>/` — existing approve,
  reject, or reset action. `rejection_reason` is required for rejection.
- The alternate admin document action endpoint follows the same photo rules.
- Generic document upload and deletion cannot bypass the photo lock.

## Deployment order

1. Apply `verifications.0012_profile_photo_type` and
   `0013_grandfather_profile_photos` together as part of the backend deployment,
   before publishing the admin or mobile changes. The Railway startup script
   runs `migrate --check`, which checks for pending migrations but does not apply
   them. Run `python manage.py migrate --noinput` in the deployment's pre-deploy
   step or release command before starting the new backend.
2. The first migration updates the existing document-type choice/check
   constraint; the second creates grandfathered photo and audit records. It
   does not claim that existing ID/clearance documents are approved.
3. Deploy the admin portal, then publish the mobile JavaScript update using the
   existing compatible OTA runtime. All mobile edits are confined to `src/`;
   no app version, package, lockfile, or Android build changes are included.
4. On a test account, reject only the photo, refresh the mobile profile, retake,
   and confirm that it becomes pending. Approve it and confirm eligibility is
   restored only if the other required credentials are approved and valid.

The data migration is idempotent and preserves history on reverse migration.
Do not reverse the document-type constraint while photo rows still exist;
restore deliberately from a backup if a full schema rollback is needed.

## Validation

Backend tests run with `--ds=Serbisure.test_settings` against a disposable local
PostgreSQL database. Historical migration fixtures restore the current schema
after exercising older migration states. Production database migrations were
not run as part of implementation.

Mobile verification commands:

```sh
npm run typecheck
npm test
node --test src/features/profile/components/profilePhoto.test.cjs
```

The focused photo tests live inside `src/` to respect the source-only mobile
scope. The current npm test script discovers `tests/`, so run that last command
explicitly for the new photo-flow tests. Admin tests include interactive photo
review, required rejection reason, broken-image handling, and failed saves.

Automated results do not replace testing the live camera on an actual phone.
No APK was built, no OTA was published, and no repository changes were pushed.
