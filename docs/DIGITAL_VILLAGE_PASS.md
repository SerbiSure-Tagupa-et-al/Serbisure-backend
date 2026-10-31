# Digital Village Pass

The booking ticket renders its QR locally with `react-native-qrcode-svg`. It
encodes the signed `pass_url` returned by Django: `/verify/?token=<signed_token>`.
The existing short
`BK-XXXXXXXX` remains a logbook reference and is never used to look up a pass.
No models, columns, statuses, or migrations were added.

## Mobile

- `BookingPassQRCode.tsx` replaces the decorative barcode in the shared
  `BookingOfferReview.tsx`, covering both the review and Booking Details ticket.
- Drafts have a placeholder. A saved signed URL produces a black-on-white QR,
  error correction M, and a white quiet zone. Generation makes no network call.
- Tap either the QR or the short code to copy the short code. Failed clipboard
  operations show an honest message instead of claiming success.
- Booking data must already be available on the device. This does not introduce
  offline synchronization or caching for booking lists.
- Opening Booking Details attempts an authenticated refresh. The saved URL is
  displayed immediately and retained if that request fails offline. Old tickets
  without a signed URL ask the user to reconnect and reopen the booking.
- Expo SDK 51 uses `react-native-svg` 15.2.0 and `expo-clipboard` ~6.0.3. These
  native modules require a new development/release build before device testing.
  A Metro reload or JavaScript-only OTA update cannot add them to an old APK.
- React Native 0.74's missing TextEncoder is polyfilled only when necessary.

## Public Django page

`GET /verify/?token=<signed_token>` renders `booking/pass_details.html` without login.

- `generate_gate_pass_token()` uses Django `signing.dumps` with salt
  `booking.gate_pass.v1`; the view uses `signing.loads(..., max_age=86400)`.
  The 24 hours starts when Django issues the URL, not when it is screenshotted.
  Refreshing the ticket online creates a fresh URL without extending old links.
- Only the booking poster, direct recipient, or assigned partner receives
  `pass_url` from `BookingDetailSerializer`. Public feed responses never mint one.
- Expired signatures return a friendly HTTP 410 with instructions to open
  SerbiSure online for a new pass. Missing, altered, malformed, or incorrectly
  signed tokens return HTTP 404 before any booking lookup.
- Legacy `/verify/<uuid>/` and short-code URLs return HTTP 404. They cannot mint
  or redirect to fresh tokens; that would bypass the 24-hour limit.
- Signing protects against tampering and forgery; it does not encrypt the UUID
  inside the encoded token. A copied valid link works until its original expiry.

- Accepted/InProgress + an assigned worker/homeowner + active, currently
  verified accounts + a complete address/schedule + the current permitted
  Philippine work date and hours: green **OK to enter**.
- Confirmed future bookings, rest days, or times outside daily hours: amber,
  **Do not enter yet**, with the agreed schedule shown.
- Pending: amber, **Do not enter yet**.
- Cancelled/Completed or elapsed end time: red, **Do not enter**.
- Missing/invalid references: friendly 404. Missing assignments or invalid
  participant roles never authorize entry.
- Open-ended engagements still respect the start date, selected working days,
  and daily hours. Missing multi-day hours/days are never invented.
- Pending, cancelled, expired, invalid, and inactive-participant responses omit
  names, photos, house addresses, and phone numbers. Confirmed active bookings
  show the worker and resident, exact booking address, and homeowner call button.
- Salary, payment values, emails, government ID numbers, and document images
  are never included in the public template context.
- Responses disable caching, indexing, framing, unapproved scripts, and referrer sharing.
  A basic per-IP throttle limits lookups. Database failures deny authorization.
- The page never marks a booking started or records an arrival. This is a live
  reference check, not a one-time credential; someone holding a valid URL can
  share it. Guards must check the official domain, refresh, and match the person
  with the displayed photo/name and physical ID.

## Deployment and testing

Deploy the Django route before distributing the updated mobile build. URLs use
the existing configured backend origin: the local LAN backend in development,
and the current Railway backend in release builds. The example `serbisure.com`
domain will work only if it is actually configured to serve this Django route.

The worker can display the QR without internet once the ticket data is present.
The guard needs internet for the live database check and participant photos.
Test with real budget phones before release; desktop decoding does not prove
camera scanning performance, glare tolerance, or focus speed.

Existing raw-UUID QR screenshots stop working after this route change. Open the
updated ticket online to obtain a signed QR. This signing change adds no native
modules and requires no database migration. The original QR feature's native
dependencies still need to be present in the installed APK.

Focused checks:

- Django: `booking.test_gate_pass`, `booking.test_gate_pass_tokens`, `booking.test_chat_offers`.
- Mobile: `tests/booking-pass-qr.test.cjs`, `tests/booking-ticket-dates.test.cjs`,
  `tests/production-defaults.test.cjs`, and TypeScript checking.
