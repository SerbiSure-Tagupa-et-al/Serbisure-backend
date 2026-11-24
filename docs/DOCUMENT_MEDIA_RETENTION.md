# Verification image retention

Effective date: **2026-11-25 00:00 Asia/Manila**. Every deadline uses the later
of the relevant event and this effective date. Old uploads therefore cannot be
purged before **2026-12-25**; legacy pending uploads reach 60 days on **2027-01-24**.
Later approvals, rejections, replacements and account closures start their own clocks.
`VERIFICATION_MEDIA_RETENTION_START_AT` must be a timezone-aware ISO timestamp.
Do not move it backwards to accelerate deletion.

## Scheduling

Apply `verifications.0015_document_media_retention` on the database used by the
server before starting the updated backend. A different terminal may use a
different `DATABASE_URL`; verify the target before migrating. Production was not
migrated or changed by this local implementation.

Preview only:

```console
python manage.py purge_expired_verification_media --dry-run
```

After reviewing the preview, configure the hosting platform's scheduled job to
run `python manage.py purge_expired_verification_media` at least daily, with the
same database and Cloudinary settings as the backend. Do not run it at app startup
or on API reads. This repository command alone does not install a scheduler.
No real Cloudinary cleanup was run while implementing this feature.

## Rules

- Rejected and approved credential images: 30 days from the decision audit event;
  legacy records without a decision timestamp use submission time, floored at rollout.
- Expired images: 30 days after the last valid day ends in Philippine time.
- Replaced images: 30 days from replacement, or an earlier approval/rejection deadline.
  In-place replacements and deleted rows retain their old asset IDs in an append-only
  audit event so their files can still be cleaned up.
- Pending: admin warning at 30 days; purge at 60 days. The abandoned submission becomes
  Unverified and can be uploaded again.
- Active profile photos stay available as avatars. Account closure is timestamped
  in the audit log. Images, including the current avatar, are then due after 30 days.
- Successful purge clears image references, raw OCR and structured OCR copies,
  and records `media_purged_at`. Approval/rejection status, reviewer, issue/expiry
  dates and existing audit records are preserved. Expiry rules still apply normally.
- Storage failures retain the reference for retry and produce a non-zero exit code.
  Shared live image references are skipped to avoid deleting another current image;
  inspect skipped/error counts instead of assuming cleanup succeeded.

Historical Cloudinary assets whose IDs were already overwritten without any stored
reference cannot be recovered from the database. They require a separate inventory;
this command never deletes an entire folder based only on upload age.
Provider backups need a separate retention configuration; this command manages
live Cloudinary delivery assets and cache invalidation, not backup administration.

The 30/60-day periods are SerbiSure policy, not fixed deadlines imposed by RA 10173
or NPC Advisory Opinion 2017-024. The PhilSys upload is a SerbiSure safety requirement;
RA 10361 does not itself mandate that upload. Groq's existing vision fallback may
receive document images; the privacy notice discloses this alongside text processing.

References: [NPC Advisory Opinion 2017-024](https://privacy.gov.ph/wp-content/uploads/2022/01/NPC_AdvisoryOpinionNo._2017-024.pdf)
and [RA 10361, section 17](https://lawphil.net/statutes/repacts/ra2013/ra_10361_2013.html).
