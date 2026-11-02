# Public posts use daily hours, chat contracts use calendar schedules

Public Post a Job / Post a Service submissions are advertisements. They retain
service, engagement, setup, location, rate, optional instructions, and daily start/end
hours. They do not request a calendar date or weekdays.

## Implementation

- `/api/v1/booking/post/` uses `PublicPostSerializer`. Older APK calendar fields
  (`start_time`, `end_time`, `work_days`) are ignored on this endpoint.
- The existing NOT NULL `start_time` column receives the server's current timestamp
  solely for storage compatibility. It is never an advertised service date. Using
  the current instant rather than a future shift start prevents the unchanged
  acceptance rules from treating that value as a future, agreed contract schedule.
- `end_time` is `NULL`; `work_days` is `[]`, matching the existing array schema.
  `daily_start_time` and `daily_end_time` retain the selected hours. Legacy clients
  that omit both daily times keep the existing compatibility behavior; the new
  public wizard requires valid hours before proceeding.
- No model, column, table, or migration was added or altered. Existing records are
  not rewritten. Wage, verification, throttling and idempotency checks remain.
- Public feed availability already checks Pending status, no assignment, and an
  active poster. There is no calendar cutoff; creation ordering remains unchanged.
- Public UI hides calendar dates, weekdays, and Expired badges. Own Posts retains
  cancellation and lifecycle states and removes rescheduling without a replacement.
- Existing API schedule fields/derived flags and the legacy repost endpoint remain
  for compatibility; the updated public UI does not use them to present schedules
  or rescheduling controls. Shared contract types and calendar widgets are preserved.

## Chat boundary

`BookingSerializer`, `chat_offers.py`, chat offer payloads, `BookingModal`, acceptance,
cancellation, pair limits, and application creation are unchanged. Actual future
dates and weekdays are still negotiated through the existing chat contract flow.
Applying still records the existing proposal and interest message. An advertisement's
internal timestamp does not make it an agreed future contract. Existing separately
created chat offers remain independent of their public listing.

## Rollout and verification

Deploy the backend public serializer before using the new mobile wizard, because
the older backend requires a public start date. This feature requires no migration.
No production or restored-backup database was changed during implementation.

Test both account roles posting short/long ads without dates, selecting preset or
custom hours, viewing Home/Browse/profile/own details, and cancelling an own post.
Then test a fresh dated chat offer, acceptance, cancellation, and its ticket/pass.
Detailed automated results and known pre-existing suite failures are recorded in
the workspace output file `public-posts-qa-2026-11-02.md`.
