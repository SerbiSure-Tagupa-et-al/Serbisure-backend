# Document submission and background analysis

Both Homeowner and Kasambahay submissions use `POST /api/v1/verifications/upload/`.
The same flow handles replacement of rejected documents.

1. The upload serializer validates the image and document type.
2. `services/document_upload.py` uploads the image to authenticated Cloudinary
   storage and saves a Pending document. It performs no OCR or AI calls.
3. After the database transaction commits, it starts background processing and
   returns the existing HTTP 201 response. Upload success confirms storage, not
   approval or successful AI extraction.
4. `services/document_processor.py` is the only upload analysis pipeline. It
   fetches the saved image, uses the existing OCR/AI fallbacks, compares the
   extracted details with the user's profile, and saves analysis fields.
5. Barangay/Admin review remains responsible for Verified/Rejected decisions.

The mobile app still uploads selected documents sequentially. Its existing
status polling shows checking while `ocr_processed_at` is empty, followed by
the review state. Older clients keep the same endpoint and response fields;
initial OCR values are now empty until background processing finishes.

## Ownership and retry rules

Two private nullable fields coordinate work across threads/server processes:

- `ocr_processing_token`: identifies the attempt that may write results.
- `ocr_last_attempt_at`: records when the current/latest attempt started.

The claim is protected by a short database row lock; no external API call holds
that lock. A live claim lasts up to five minutes. Status polling and the
maintenance command use the same claim guard, so they do not launch overlapping
attempts or consume retry counts while a job is active. Automatic retries are
bounded at three, with at least 30 seconds between attempt starts. Explicit
admin reprocessing can rerun completed work, but still respects a live claim.

Re-upload clears prior analysis, reviewer metadata, retry counts and ownership.
A worker can write only if both its original image and token still match.
It updates analysis fields only, never review status/reviewer/rejection notes.
Credential expiry updates retain the existing workforce history side effect.

This remains best-effort background threading, without a separate queue service.
A server restart can interrupt work. A later status request or the existing
`retry_failed_ocr` command can recover expired work; no scheduler is introduced.
Provider attempts are bounded (Groq 30 seconds per model without SDK retries;
Tesseract 20 seconds; existing Google/download timeouts remain).

## Deployment

Apply `verifications/0010_document_processing_lease.py` to the intended backend
database before starting the updated code. It adds the two nullable internal
columns; no existing columns are removed. Then restart the backend. The existing
Railway start script checks for unapplied migrations but does not apply them.

No new environment variables or production dependencies are required. Cloudinary
and OCR/AI credentials remain backend-only. Actual upload latency, provider quota,
and restart behaviour must be checked after deployment.
