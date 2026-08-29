# Serbisure API Reference

Welcome to the manual API documentation for the Serbisure Backend. This document outlines every active endpoint, the required payloads, expected security headers, and common error scenarios.

---

## 1. Accounts & Authentication

### `POST /api/v1/accounts/register/`
Registers a new user (Kasambahay or Homeowner) and prevents duplicate double-clicks via Idempotency keys. Admins are blocked from using this public endpoint.

- **Authentication:** `None`
- **Required Headers:** 
  - `Idempotency-Key`: `string (UUID v4)` (Required to prevent duplicate accounts)
  
**✅ Valid Payload (201 Created):**
```json
{
    "first_name": "Juan",
    "last_name": "Dela Cruz",
    "email": "juan@example.com",
    "password": "StrongPassword123!",
    "account_type": "Homeowner",
    "contact_number": "+639123456789"
}
```

**❌ Common Errors:**
- **400 Bad Request (Missing Header):**
  - *Response:* `{ "details": "The Idempotency-Key header is required and must be a valid UUID v4." }`
  - *Solution:* Generate a new UUIDv4 in the frontend and send it in the headers.
- **400 Bad Request (Admin Registration Attempt):**
  - *Response:* `{ "account_type": ["You cannot create an Admin account through this public endpoints"] }`
  - *Solution:* Admins must be created via the server terminal using `python manage.py createsuperuser`.
- **429 Too Many Requests (Rate Limit):**
  - *Response:* `{ "detail": "Too many attempts. Please try again in 24 hours." }`

---

### `POST /api/v1/accounts/login/`
Authenticates a user and returns a JWT access token and refresh token. Includes brute-force sliding-window protection.

- **Authentication:** `None`
- **Required Headers:** `None`

**✅ Valid Payload (200 OK):**
```json
{
    "email": "juan@example.com",
    "password": "StrongPassword123!"
}
```
*Expected Outcome:* Returns an `access` token (lasts 1 hour) and a `refresh` token (lasts 1 week).

**❌ Common Errors:**
- **401 Unauthorized (Wrong Credentials OR Admin Login Attempt):**
  - *Response:* `{ "detail": "Wrong email or password. Please try again!" }`
  - *Solution:* Ensure credentials are correct. **Note:** Admins and Barangay roles are intentionally blocked from this endpoint and will receive this generic error even with a correct password.
- **429 Too Many Requests (Brute Force Lockout):**
  - *Response:* `{ "detail": "Too many attempts. Please try again in 5 minutes." }`
  
---

### `POST /api/v1/accounts/token/refresh/`
Issues a brand new access token without making the user log in again.

- **Authentication:** `None`
- **Required Headers:** `None`

**✅ Valid Payload (200 OK):**
```json
{
    "refresh": "<your-refresh-token-string-here>"
}
```

---

## 2. Document Verifications

### `POST /api/v1/verifications/upload/`
Uploads an identity document (ID) to Cloudinary and saves the reference to the database for Admin review. 

- **Authentication:** `Required (JWT Access Token)`
- **Required Headers:** 
  - `Authorization`: `Bearer <your_access_token>`
- **Content-Type:** `multipart/form-data` (Since you are uploading a physical file)

**✅ Valid Payload (201 Created):**
- `document_type`: `string` (e.g., "national_id_front")
- `document_image`: `File` (The actual image file)

*Note: `date_issued` and `valid_until` are Read-Only for security. Only Admins can set these fields later.*

**❌ Common Errors:**
- **401 Unauthorized:**
  - *Response:* `{ "detail": "Authentication credentials were not provided." }`
  - *Solution:* Include the `Authorization: Bearer <token>` in the header.

---

## 3. Booking & Jobs

### `POST /api/v1/booking/post/`
Creates a new job posting or service booking. Both Kasambahays and Homeowners can post. 

- **Authentication:** `Required (JWT Access Token)`
- **Required User State:** User's `verification_status` MUST be `"Verified"` in the database.
- **Required Headers:**
  - `Authorization`: `Bearer <your_access_token>`
  - `Idempotency-Key`: `string (UUID v4)` (Required to prevent double-posting)

**✅ Valid Payload (201 Created):**
```json
{
    "booking_type": "short_term",
    "service_category": "Cleaning",
    "start_time": "2026-12-01T09:00:00Z",
    "end_time": "2026-12-01T14:00:00Z",
    "service_address": "123 Main Street, Manila",
    "special_instruction": "Please bring your own vacuum cleaner."
}
```

**❌ Common Errors:**
- **403 Forbidden (Unverified User):**
  - *Response:* `{ "detail": "Only verified user can post" }`
  - *Solution:* The user's account must be manually verified by an Admin in the backend before they are allowed to create bookings.
- **400 Bad Request (Time Travel):**
  - *Response:* `{ "start_time": ["Start time must be in the future."] }`
  - *Solution:* Ensure both `start_time` and `end_time` are future dates.
- **400 Bad Request (Invalid Logic):**
  - *Response:* `{ "end_time": ["End time must be strictly after the start time."] }`

---

## 4. Reviews & Ratings

### `POST /api/v1/reviews/create/`
Submits a rating and feedback review for a completed booking. Reviewer must be a verified participant (poster or accepter) of the booking. Automatically derives the reviewee.

- **Authentication:** `Required (JWT Access Token)`
- **Required User State:** User's `verification_status` MUST be `"Verified"`.
- **Required Headers:**
  - `Authorization`: `Bearer <your_access_token>`
  - `Idempotency-Key`: `string (UUID v4)` (Required to prevent duplicate reviews)

**✅ Valid Payload (201 Created):**
```json
{
    "booking_id": "c1f7b0f6-9f87-4b71-93c6-6b215e9e0321",
    "rating": 5,
    "unstructured_feedback": "Excellent service! Very prompt and thorough cleaning.",
    "nlp_sentiment": "Positive"
}
```

**❌ Common Errors:**
- **403 Forbidden (Unverified User):**
  - *Response:* `{ "detail": "Only verified users can submit reviews." }`
- **400 Bad Request (Booking Not Completed):**
  - *Response:* `{ "booking_id": ["You can only submit a review for bookings that are marked as 'Completed'."] }`
- **400 Bad Request (Not a Participant):**
  - *Response:* `{ "detail": "You are not an authorized participant (poster or accepter) of this booking." }`
- **400 Bad Request (Duplicate Review):**
  - *Response:* `{ "detail": "You have already submitted a review for this booking." }`

---

### `GET /api/v1/reviews/received/`
Retrieves all reviews received by the authenticated user.

- **Authentication:** `Required (JWT Access Token)`
- **Headers:** `Authorization: Bearer <your_access_token>`

---

### `GET /api/v1/reviews/given/`
Retrieves all reviews submitted by the authenticated user.

- **Authentication:** `Required (JWT Access Token)`
- **Headers:** `Authorization: Bearer <your_access_token>`

---

### `GET /api/v1/reviews/summary/<uuid:user_id>/`
Returns aggregated review statistics for a target user (average rating, count, rating breakdown, sentiment distribution).

- **Authentication:** `Required (JWT Access Token)`
- **Headers:** `Authorization: Bearer <your_access_token>`

---

### `GET /api/v1/reviews/user/<uuid:user_id>/`
Retrieves the public list of reviews received by a target user.

- **Authentication:** `Required (JWT Access Token)`
- **Headers:** `Authorization: Bearer <your_access_token>`
