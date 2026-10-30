# Review sentiment flow

Mobile submits authenticated feedback, stars, booking ID, and a retry key to Django. Django verifies the reviewer and completed booking, calls the Hugging Face Space with its private token, validates the complete prediction event, and saves the review. Mobile displays the saved server result. Stars never determine sentiment.

## Configuration and migration

- Backend environment: `HUGGINGFACE_API_TOKEN` (or `HF_TOKEN`) and `HUGGINGFACE_SENTIMENT_SPACE_URL`. The token must have access to the configured Space. Never put it in `EXPO_PUBLIC_*` variables.
- Apply Django migrations, including `reviews.0003_tbl_review_sentiment_analysis_and_more`, on each target database before starting the updated backend. The local development database has been migrated.
- Restart Django after updating backend code. Cloud deployment is separate; configure its private environment and ensure its request execution limit accommodates the model's 40-second processing budget plus connection overhead and stalled-stream detection. The initial prediction request can wait up to 30 seconds for a response; a stream that stops sending data times out after at most 15 seconds. Mobile already waits up to 60 seconds and needs no change for this adjustment.
- Model source is recorded as `Rhoii/cebuano-taglish-sentiment`, the model used by the configured SerbiSure Space. When changing that Space's model, update this configured identifier too. Metadata records source, label, confidence, probabilities, Space URL, and analysis timestamp; it is not a pinned model artifact revision.

## User-visible states

1. While writing: no guessed sentiment and no repeated prediction requests.
2. Submit: spinner and **Analyzing your feedback…**. Inputs and close are disabled during submission. After eight seconds, **Still checking. Please keep this screen open for a moment.** No fabricated percentage is shown.
3. Success: **Review submitted**, the actual saved Positive / Negative / Neutral result, and **Done**. Star rating remains separate.
4. Provider quota, network, malformed result, or timeout: no review is saved as a fallback. Django returns HTTP 503 with `sentiment_unavailable`; mobile preserves feedback and shows **Analysis is unavailable right now** and **Try Again**.
5. Mobile loses the response: retrying unchanged feedback with the same key returns the saved review if it already exists. The key is scoped to the reviewer and stored in PostgreSQL. A different body cannot reuse a saved key. Existing booking/reviewer uniqueness is also retained.

Backend `sentiment_api` logs identify the stage (`submit_prediction`, `wait_prediction`, or `parse_prediction`), elapsed time, failure reason, HTTP status when received, and exception type for network/parsing errors. They exclude credentials, review text, event IDs, raw exception messages, and provider response bodies. Stream read timeouts wrapped as connection errors are recorded as `timeout`.

The request is synchronous: there are no invisible pending review rows or background tasks. A review becomes public only after valid analysis and a successful database save.

## Legacy review repair

Existing labels have empty provenance metadata. They cannot be assumed to be genuine model predictions. The `reanalyze_reviews` management command targets only these records, defaults to a dry run, accepts repeatable `--review-id` options and a bounded `--limit`, and requires `--apply` to persist results. Failures retain the original label. Successful repair retains the original label as `sentiment_analysis.legacy_label`; stars and original feedback remain unchanged.

## QA checks

- Test homeowner and worker reviews on a completed booking; unauthorized participants, pending bookings, and unverified accounts must fail before inference.
- Send forged client `nlp_sentiment` values: only the model prediction may be saved.
- Test HTTP 200 streams containing `event: error`, incomplete streams, unsupported labels, non-finite confidence, inconsistent probabilities, private token omission, and provider network failure. None may create Neutral as a fallback.
- Retry after clearing application cache; verify a single saved row and the same review ID. Reusing another reviewer's key must not expose their response.
- Compare **exactly the same text and capitalization** against the Space website. Live tests on October 12, 2026 returned Negative / 0.7086 for `Pangit ug batasan`, Negative / 0.7271 for `Dili kabalo mo lipyo`, Positive / 0.921 for lowercase `syada siya mo hatag ug sweldo`, and Negative / 0.7259 for `Syada Siya mo hatag ug sweldo`. This capitalization sensitivity comes from the deployed model. Django trims surrounding whitespace but preserves case and wording; it does not substitute a preferred prediction.

Automated tests cover provider parsing, token handling, failure safety, real server-owned labels, durable replay, legacy repair, and mobile request/error behavior. Native visual behavior still needs checking on the test device.
