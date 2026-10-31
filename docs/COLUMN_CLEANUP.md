# Six obsolete columns

Removed: `tbl_booking.cancel_requested_at`, `tbl_booking.cancel_requested_by`,
`tbl_documents.face_liveness_score`, `tbl_chat_reaction.reacted_at`,
`accounts_tbl_user_profile.resume_url`, `accounts_tbl_user_profile.resume_uploaded_at`.

The old uploaded-PDF `/api/v1/accounts/resume/` endpoint is retired (404), and
public profiles no longer include `resume_url`. Current mobile CV screens use
`worker-cv/` and its exports; those remain unchanged. Old clients using uploaded
PDF resumes must move to the current CV flow. Mobile unused helpers/types were
removed. The admin has no consumers of these columns and needs no changes.

Historical migrations stay intact so new databases can still be created.
Booking's old cancellation-request columns exist only in older database schemas,
not in the current migration state; its database-only cleanup uses `IF EXISTS`
without `CASCADE`. Unexpected database dependencies will stop the operation.

Deployment: use a maintenance window to stop backend writes, keep the SQL backup,
review `python manage.py migrate --plan`, apply `python manage.py migrate --noinput`
with the new release code and the intended database settings, then restart that
release. Do not run the old backend against the cleaned schema. In particular,
reaction timestamps were NOT NULL: coordinate code and migration deployment so
neither version writes against an incompatible schema.

Migration files: accounts `0025`, booking `0019`, chat `0012`, verifications `0011`.
This also requires any earlier pending migrations in their dependency chain.
Column drops delete old values, including existing reaction timestamps; rollback
of removed data requires the backup. Booking's database-only migration is
explicitly irreversible. No migration has been applied to Neon by this change.

After deployment check registration/login, generated CVs, upload/renewal,
reactions, cancellation, applications, and the admin verification queue.
