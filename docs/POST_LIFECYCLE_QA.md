# Independent public posts and chat offers

## Rules

- Public, unbooked Pending posts stay visible regardless of their original service date. Existing account and rate-policy safeguards remain in effect. No age-based hiding, automatic deletion, or scheduled expiration runs.
- Feed order is always newest creation first (`createdAt` descending); old price-sort parameters cannot reorder it. Category, location and compensation filters still work.
- `schedule_expired` is calculated for the API response, never stored. The badge uses Philippine calendar dates: a start date before today shows Expired Schedule. The exact scheduled start instant remains the acceptance deadline, so an already elapsed time today also cannot be accepted.
- Apply Now uses the existing proposal table, saves one application, and persists the interest chat message in the same transaction. Repeated or concurrent taps do not add duplicate applications, messages or counters. Date passage alone does not close an application.
- Accept remains available only to the proper participant and only while the original schedule is still in the future. Passed offers keep Decline enabled and show: "Cannot accept: Original date has passed. Please send a new offer in chat with an updated date."
- A new offer sent through chat is an independent booking. Accepting Job B does not modify, close, assign or reject applications on Job Post A. A remains Pending until its owner manually cancels it or accepts a valid application on A itself.
- Own Recent Posts includes public history. Cancel Post calls the existing cancellation endpoint with `pending_only: true`, a confirmation and zero expected points. The server rechecks this condition under a row lock so a stale action cannot accidentally cancel an accepted contract. Existing contract cancellation behaviour is unchanged.
- Existing owner reposting creates a separate post without transferring applications. Under the independent-post rules, the owner manually cancels the original listing when it is no longer needed.
- The existing five booking statuses and existing proposal statuses are unchanged. No columns or tables are added for these rules.

## Source-link removal

The `source_post` model field and all offer-to-feed linking logic have been removed. Mobile requests no longer send `source_post_id`. Older request payloads containing it are ignored for compatibility and never create a link.

Migration `booking/0017_remove_booking_source_post.py` removes the database column. Migration 0016 is kept in history because another checkout/database may already have applied it. A fresh database runs both migrations and finishes with no source column.

Run this from the activated backend environment pointing at the intended database before testing the updated code:

```cmd
python manage.py migrate booking
```

Do not manually drop the column in PostgreSQL while running code that still declares or queries it; that would cause runtime errors. This implementation removes model, serializer, service and query dependencies together and uses Django's removal migration.

During implementation the migration was executed and checked only on disposable local PostgreSQL test databases. Live and restored-backup databases were not changed. No APK was built.

## Validation

Backend coverage includes final schema inspection (source column absent), past short/long posts, creation-order sorting even with legacy sort parameters, application persistence/retries, expired acceptance rejection, valid acceptance, competing applicants, concurrent acceptance, independent Job B acceptance, owner-only cancellation and its acceptance race, and realtime isolation.

Mobile coverage includes Philippine date badges, active past-date application labels, gray disabled Accept controls in chat and details, an authoritative local action check, future acceptance, retry behaviour, foreground/deadline updates, Cancel Post confirmation/duplicate taps and unchanged Home/Explore/Profile application flows. Focused TypeScript, Ruff and migration consistency checks are run. Physical phone testing is still needed with the updated backend.
