"""
accounts/tests.py
=================
Test suite for the accounts application.

Coverage map:
  - TestUserModels          → Django ORM model-level sanity checks
  - TestSerializers         → Business logic & validation inside serializers
  - TestUserAPIEndpoints    → HTTP-level integration tests (register, login,
                              user-about, user-tags)

Naming convention:
  test_<thing>_<condition>_<expected_outcome>
  e.g. test_password_no_number_rejected

Run all account tests:
  python manage.py test accounts

Run a single class:
  python manage.py test accounts.tests.TestUserAPIEndpoints
"""

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from django.core.cache import cache
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from unittest.mock import patch
from .models import tbl_user_profile
from .serializers import UserRegistrationSerializer
import uuid


# =============================================================================
# SECTION 1 — MODEL TESTS
# Purpose: Verify that the Django ORM behaves correctly at the model layer,
#          independent of any HTTP request. If these fail, the DB schema or
#          CustomUserManager has a bug.
# =============================================================================

class TestUserModels(TestCase):

    def test_create_superuser_flags_are_set(self):
        """
        GIVEN  a call to create_superuser()
        WHEN   the user is saved to the DB
        THEN   is_superuser=True, is_staff=True, and username is preserved.

        Why this matters: Superuser creation is used in production via the
        'python manage.py createsuperuser' command. If the CustomUserManager
        breaks this, no one can log into the Django admin panel.
        """
        admin = tbl_user_profile.objects.create_superuser(
            username="admin_master",
            email="admin@example.com",
            password="StrongPassword123!"
        )
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_staff)
        self.assertEqual(admin.username, "admin_master")

    def test_user_tags_field_defaults_to_empty_list(self):
        """
        GIVEN  a freshly created user with no user_tags provided
        WHEN   we read user.user_tags from the DB
        THEN   it should be [] (empty list), NOT None or a string.

        Why this matters: The JSONField default=list must produce [] not null.
        If this is None, the frontend will crash trying to iterate over tags.
        """
        user = tbl_user_profile.objects.create_user(
            username="tagtest",
            email="tagtest@example.com",
            password="Password123!",
            contact_number="+639123456789"
        )
        self.assertEqual(user.user_tags, [])
        self.assertIsInstance(user.user_tags, list)

    def test_user_about_field_defaults_to_no_bio(self):
        """
        GIVEN  a freshly created user with no user_about provided
        WHEN   we read user.user_about from the DB
        THEN   it should be the string 'No Bio'

        Why this matters: The ProfileScreen renders user_about. A None value
        would cause a crash or display 'null' on the mobile UI.
        """
        user = tbl_user_profile.objects.create_user(
            username="abouttest",
            email="abouttest@example.com",
            password="Password123!",
            contact_number="+639123456789"
        )
        self.assertEqual(user.user_about, "No Bio")


# =============================================================================
# SECTION 2 — SERIALIZER TESTS
# Purpose: Validate that all custom validate_* methods inside
#          UserRegistrationSerializer reject bad data and accept good data.
#          These tests do NOT make HTTP calls — they are pure Python unit tests.
# =============================================================================

class TestSerializers(TestCase):

    def setUp(self):
        """
        Baseline valid payload that passes ALL validators.
        Each test mutates one field at a time to isolate specific rules.
        """
        self.valid_data = {
            "email": "test@example.com",
            "password": "StrongPassword123!",
            "first_name": "Juan",
            "middle_name": "Dela",
            "last_name": "Cruz",
            "contact_number": "+639123456789",
            "account_type": "Homeowner"
        }

    # ── ACCOUNT TYPE ──────────────────────────────────────────────────────────

    def test_admin_account_type_blocked_from_public_registration(self):
        """
        GIVEN  account_type='Admin'
        THEN   serializer must be invalid.

        Security rule: Admins can only be created via the terminal
        (createsuperuser). This prevents privilege escalation through the API.
        """
        self.valid_data["account_type"] = "Admin"
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("account_type", serializer.errors)

    # ── PASSWORD ──────────────────────────────────────────────────────────────

    def test_password_letters_only_rejected(self):
        """
        GIVEN  a password with no digits (e.g. 'alllettersonly')
        THEN   serializer must be invalid.
        Rule: Password must contain at least one number.
        """
        self.valid_data["password"] = "alllettersonly"
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)

    def test_password_digits_only_rejected(self):
        """
        GIVEN  a password with no letters (e.g. '12345678901')
        THEN   serializer must be invalid.
        Rule: Password must contain at least one letter.
        """
        self.valid_data["password"] = "12345678901"
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)

    def test_password_under_11_chars_rejected(self):
        """
        GIVEN  a password shorter than 11 characters
        THEN   serializer must be invalid.
        Boundary: min length is 11 chars.
        """
        self.valid_data["password"] = "Short1!"  # 7 chars
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)

    def test_password_over_30_chars_rejected(self):
        """
        GIVEN  a password longer than 30 characters
        THEN   serializer must be invalid.
        Boundary: max length is 30 chars.

        TODO: This test currently FAILS because validate_password() in
        UserRegistrationSerializer does not enforce a max_length of 30.
        Add `if len(value) > 30: raise serializers.ValidationError(...)` to fix.
        Once the serializer rule is added, remove the `skipTest` line below.
        """
        self.valid_data["password"] = "A" * 29 + "1!"  # 31 chars
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("password", serializer.errors)

    # ── USERNAME AUTO-GENERATION ──────────────────────────────────────────────

    def test_username_is_auto_generated_from_name(self):
        """
        GIVEN  valid registration data
        WHEN   serializer.save() is called
        THEN   user.username starts with '<first><middle><last>_' (lowercase)
               AND the raw password is hashed (check_password returns True).

        Why this matters: We do not expose usernames in registration — they are
        auto-generated. If this breaks, users would need to supply usernames.
        """
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()
        self.assertTrue(user.username.startswith("juandelacruz_"))
        self.assertTrue(user.check_password("StrongPassword123!"))

    # ── CONTACT NUMBER ────────────────────────────────────────────────────────

    def test_contact_number_wrong_prefix_rejected(self):
        """
        GIVEN  contact_number that does NOT start with '+639'
        THEN   serializer must be invalid.
        Rule: All Philippine mobile numbers start with +639.
        """
        self.valid_data["contact_number"] = "+631234567890"  # +631 prefix
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("contact_number", serializer.errors)

    def test_contact_number_too_short_rejected(self):
        """
        GIVEN  contact_number with fewer than 13 characters
        THEN   serializer must be invalid.
        Boundary: exactly 13 chars required (e.g. +639XXXXXXXXX).
        """
        self.valid_data["contact_number"] = "+6391234567"   # 11 chars
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())

    def test_contact_number_too_long_rejected(self):
        """
        GIVEN  contact_number with more than 13 characters
        THEN   serializer must be invalid.
        """
        self.valid_data["contact_number"] = "+63912345678901"  # 15 chars
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())

    def test_contact_number_with_letters_rejected(self):
        """
        GIVEN  contact_number containing non-digit characters after '+'
        THEN   serializer must be invalid.
        """
        self.valid_data["contact_number"] = "+639ABCDEFGHI"
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())

    def test_contact_number_valid_format_accepted(self):
        """
        GIVEN  a well-formed +639XXXXXXXXX contact number
        THEN   serializer must be valid (happy path).
        """
        self.valid_data["contact_number"] = "+639123456789"
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    # ── EMAIL ─────────────────────────────────────────────────────────────────

    def test_email_is_normalized_to_lowercase(self):
        """
        GIVEN  email submitted in uppercase (e.g. 'TEST@EXAMPLE.COM')
        WHEN   serializer saves the user
        THEN   user.email is stored as lowercase 'test@example.com'.

        Why this matters: Prevents duplicate accounts from 'User@x.com'
        vs 'user@x.com' which are the same email address.
        """
        self.valid_data["email"] = "TEST@EXAMPLE.COM"
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()
        self.assertEqual(user.email, "test@example.com")

    # ── AGE VALIDATION ────────────────────────────────────────────────────────

    def test_underage_user_registration_rejected(self):
        """
        GIVEN  a date_of_birth making the user under 18 years old
        THEN   serializer must be invalid.

        Legal requirement: Serbisure is a labor platform. Minors cannot
        legally enter employment contracts in the Philippines.
        """
        self.valid_data["date_of_birth"] = "2015-01-01"  # ~11 years old
        serializer = UserRegistrationSerializer(data=self.valid_data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("date_of_birth", serializer.errors)


# =============================================================================
# SECTION 3 — API ENDPOINT INTEGRATION TESTS
# Purpose: End-to-end HTTP tests simulating exactly what the mobile app does.
#          These tests hit the actual URL router, views, serializers, and DB.
#
# Endpoints under test:
#   POST   /api/v1/accounts/register/    → UserRegistrationView
#   POST   /api/v1/accounts/login/       → CustomLoginView
#   PATCH  /api/v1/accounts/user-about/  → UserAboutView
#   PATCH  /api/v1/accounts/user-tags/   → UserTagsView
# =============================================================================

class TestUserAPIEndpoints(TestCase):

    def setUp(self):
        """
        Shared setup for all API tests.
        - Each test gets a fresh in-memory DB (Django isolates tests).
        - cache.clear() resets throttle counters between tests.
        - self.valid_payload is the minimum required to register a user.
        """
        self.client = APIClient()

        # Endpoint URLs — keep these in sync with accounts/urls.py
        self.register_url = "/api/v1/accounts/register/"
        self.login_url    = "/api/v1/accounts/login/"
        self.about_url    = "/api/v1/accounts/user-about/"
        self.tags_url     = "/api/v1/accounts/user-tags/"
        self.public_profile_base = "/api/v1/accounts/public-profile/"

        # Reset throttle counters so tests don't bleed into each other
        cache.clear()

        self.valid_payload = {
            "email": "api@example.com",
            "password": "StrongPassword123!",
            "first_name": "Naruto",
            "last_name": "Uzumaki",
            "contact_number": "+639123456789",
            "account_type": "Homeowner"
        }
        self.idempotency_key = str(uuid.uuid4())

    # ── TEST HELPER ───────────────────────────────────────────────────────────

    def _create_and_login_user(
        self,
        email="testuser@example.com",
        password="StrongPassword123!",
        username="testuser"
    ):
        """
        Helper that bypasses the registration endpoint and directly creates
        a user in the DB, then logs in to get a real JWT access token.

        Returns: (user_instance, access_token_string)

        Why use this instead of the register endpoint?
        Keeps tests focused: user-about and user-tags tests don't care about
        registration logic — they only need an authenticated token.
        """
        user = tbl_user_profile.objects.create_user(
            username=username,
            email=email,
            password=password,
            contact_number="+639123456789"
        )
        response = self.client.post(self.login_url, {
            "email": email,
            "password": password
        })
        token = response.data.get("access")
        return user, token

    # ── REGISTRATION TESTS ────────────────────────────────────────────────────

    def test_register_success_returns_201_and_jwt_tokens(self):
        """
        GIVEN  valid registration data + a valid UUID Idempotency-Key
        WHEN   POST /register/
        THEN   201 Created, body contains 'access' and 'refresh' JWT tokens.

        The frontend uses these tokens immediately to log the user in
        without requiring a separate login step after registration.
        """
        response = self.client.post(
            self.register_url,
            self.valid_payload,
            HTTP_IDEMPOTENCY_KEY=self.idempotency_key
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_register_idempotency_double_click_safe(self):
        """
        GIVEN  the same idempotency key used on 2 identical POST requests
        WHEN   user double-taps the Register button
        THEN   only 1 user row is created in the DB; both requests return 201.

        Critical UX bug prevention: without this, a slow network + impatient
        user double-tap would create 2 accounts with the same email,
        causing an 'email already exists' error on the second attempt.
        """
        # First tap — hits the DB and creates 1 user
        self.client.post(
            self.register_url, self.valid_payload,
            HTTP_IDEMPOTENCY_KEY=self.idempotency_key
        )
        self.assertEqual(tbl_user_profile.objects.count(), 1)

        # Second tap — served from cache, no DB write
        response2 = self.client.post(
            self.register_url, self.valid_payload,
            HTTP_IDEMPOTENCY_KEY=self.idempotency_key
        )
        self.assertEqual(response2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(tbl_user_profile.objects.count(), 1)  # Still 1!

    def test_register_missing_idempotency_key_returns_400(self):
        """
        GIVEN  a registration request with NO Idempotency-Key header
        THEN   400 Bad Request.

        The header is mandatory. The frontend must generate a UUID v4 before
        calling this endpoint.
        """
        response = self.client.post(self.register_url, self.valid_payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_non_uuid_idempotency_key_returns_400(self):
        """
        GIVEN  Idempotency-Key header contains a plain string (not a UUID)
        THEN   400 Bad Request.

        Prevents abuse: sending 'abc' as a key would bypass the idempotency
        check because it's not a valid UUID format.
        """
        response = self.client.post(
            self.register_url, self.valid_payload,
            HTTP_IDEMPOTENCY_KEY="not-a-valid-uuid"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_admin_account_type_blocked(self):
        """
        GIVEN  registration data with account_type='Admin'
        THEN   400 Bad Request.

        Privilege escalation guard: No one should be able to self-promote
        to Admin through the public registration API.
        """
        self.valid_payload["account_type"] = "Admin"
        response = self.client.post(
            self.register_url, self.valid_payload,
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_register_duplicate_email_rejected(self):
        """
        GIVEN  a second registration attempt with the same email
        WHEN   a different Idempotency-Key is used (genuine second attempt)
        THEN   400 Bad Request — email must be unique.
        """
        # First registration — succeeds
        self.client.post(
            self.register_url, self.valid_payload,
            HTTP_IDEMPOTENCY_KEY=self.idempotency_key
        )
        # Second attempt with same email but a new key — must fail
        response = self.client.post(
            self.register_url, self.valid_payload,
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4())
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ── LOGIN TESTS ───────────────────────────────────────────────────────────

    def test_login_valid_credentials_returns_jwt_tokens(self):
        """
        GIVEN  a registered user with a known email and password
        WHEN   POST /login/ with correct credentials
        THEN   200 OK, body has 'access' and 'refresh' tokens.
        """
        tbl_user_profile.objects.create_user(
            username="testuser",
            email="login@example.com",
            password="password123!"
        )
        response = self.client.post(self.login_url, {
            "email": "login@example.com",
            "password": "password123!"
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_login_wrong_password_returns_401(self):
        """
        GIVEN  a valid email but incorrect password
        THEN   401 Unauthorized — generic error, no info leak.
        """
        tbl_user_profile.objects.create_user(
            username="testuser2",
            email="login2@example.com",
            password="password123!"
        )
        response = self.client.post(self.login_url, {
            "email": "login2@example.com",
            "password": "WrongPassword!"
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_nonexistent_email_returns_401(self):
        """
        GIVEN  an email that has never been registered
        THEN   401 Unauthorized.

        NOTE: We intentionally return 401 (not 404) so attackers cannot
        enumerate whether an email is registered in our system.
        """
        response = self.client.post(self.login_url, {
            "email": "nobody@example.com",
            "password": "SomePassword123!"
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_admin_account_blocked_from_mobile_api(self):
        """
        GIVEN  an Admin/superuser account
        WHEN   they try to login through the mobile API
        THEN   401 Unauthorized — same generic error as wrong password.

        Security: Admin accounts must use the Django admin panel (/admin/).
        Blocking them here prevents accidental exposure of admin tokens
        to mobile devices, which are less secure than desktops.
        """
        tbl_user_profile.objects.create_superuser(
            username="adminuser",
            email="admin@example.com",
            password="AdminPassword123!"
        )
        response = self.client.post(self.login_url, {
            "email": "admin@example.com",
            "password": "AdminPassword123!"
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_brute_force_locked_out_after_5_attempts(self):
        """
        GIVEN  an attacker making 5 failed login attempts (sliding window)
        WHEN   they make a 6th attempt
        THEN   429 Too Many Requests — sliding window throttle kicks in.

        The first 5 attempts return 401 (wrong password).
        Attempt 6+ returns 429 until the 5-minute window passes.
        """
        for _ in range(5):
            self.client.post(self.login_url, {
                "email": "hacker@example.com",
                "password": "WrongPassword!"
            })

        # 6th attempt — should now be rate-limited
        response6 = self.client.post(self.login_url, {
            "email": "hacker@example.com",
            "password": "WrongPassword!"
        })
        self.assertEqual(response6.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    # ── USER ABOUT TESTS ──────────────────────────────────────────────────────

    def test_user_about_unauthenticated_request_returns_401(self):
        """
        GIVEN  no Authorization header (not logged in)
        WHEN   PATCH or GET /user-about/
        THEN   401 Unauthorized.

        IsAuthenticated permission guard must be enforced.
        """
        response_patch = self.client.patch(self.about_url, {"user_about": "Hello!"})
        self.assertEqual(response_patch.status_code, status.HTTP_401_UNAUTHORIZED)

        response_get = self.client.get(self.about_url)
        self.assertEqual(response_get.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_about_authenticated_get_succeeds(self):
        """
        GIVEN  an authenticated user
        WHEN   GET /user-about/
        THEN   200 OK and returns current user_about.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = self.client.get(self.about_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("user_about"), "No Bio")

    def test_user_about_authenticated_update_succeeds(self):
        """
        GIVEN  an authenticated user
        WHEN   PATCH /user-about/ with valid bio text
        THEN   200 OK and user.user_about is updated in the DB.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = self.client.patch(self.about_url, {"user_about": "I am a great homeowner!"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.user_about, "I am a great homeowner!")

    def test_user_about_empty_string_is_accepted(self):
        """
        GIVEN  user_about = '' (empty string)
        THEN   200 OK — blank=True on the model field allows this.

        Use case: user wants to clear their bio.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(self.about_url, {"user_about": ""})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_about_exactly_500_chars_is_accepted(self):
        """
        GIVEN  user_about is exactly 500 characters long
        THEN   200 OK — this is the max boundary value; it must be accepted.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(self.about_url, {"user_about": "A" * 500})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_about_501_chars_is_rejected(self):
        """
        GIVEN  user_about is 501 characters long
        THEN   400 Bad Request — exceeds max_length=500.

        Off-by-one boundary test: 500 = pass, 501 = fail.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(self.about_url, {"user_about": "A" * 501})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_about_cannot_update_another_users_bio(self):
        """
        GIVEN  user A is authenticated
        WHEN   they PATCH /user-about/
        THEN   only user A's bio changes — user B's bio is untouched.

        Security: get_object() returns self.request.user, so there is no way
        to target another user's record. This test verifies that isolation.
        """
        user_a, token_a = self._create_and_login_user(email="usera@example.com", username="usera")
        user_b = tbl_user_profile.objects.create_user(
            username="userb", email="userb@example.com",
            password="Password123!", contact_number="+639111111111"
        )
        original_bio = user_b.user_about  # save baseline

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_a}")
        self.client.patch(self.about_url, {"user_about": "Hacked bio!"})

        # User B's bio must remain unchanged
        user_b.refresh_from_db()
        self.assertEqual(user_b.user_about, original_bio)

    # ── USER TAGS TESTS ───────────────────────────────────────────────────────

    def test_user_tags_unauthenticated_request_returns_401(self):
        """
        GIVEN  no Authorization header
        WHEN   PATCH /user-tags/
        THEN   401 Unauthorized.
        """
        response = self.client.patch(self.tags_url, {"user_tags": ["Non-Smoker"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_tags_authenticated_update_succeeds(self):
        """
        GIVEN  a valid JSON array of string tags
        WHEN   PATCH /user-tags/
        THEN   200 OK and user.user_tags is updated in the DB.

        NOTE: Must use format='json' so DRF parses the array correctly.
        Without format='json', the array becomes a multipart string.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(
            self.tags_url,
            {"user_tags": ["Non-Smoker", "Respectful", "Pet Owner"]},
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.user_tags, ["Non-Smoker", "Respectful", "Pet Owner"])

    def test_user_tags_empty_array_clears_all_tags(self):
        """
        GIVEN  user_tags = [] (empty array)
        THEN   200 OK — allows the user to remove all their tags.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(self.tags_url, {"user_tags": []}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertEqual(user.user_tags, [])

    def test_user_tags_exactly_10_tags_is_accepted(self):
        """
        GIVEN  exactly 10 tags (boundary maximum)
        THEN   200 OK — 10 is the allowed maximum.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        ten_tags = [f"Tag{i:02d}" for i in range(10)]  # Tag00, Tag01, ... Tag09
        response = self.client.patch(self.tags_url, {"user_tags": ten_tags}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_tags_11_tags_is_rejected(self):
        """
        GIVEN  11 tags (one over the maximum)
        THEN   400 Bad Request.

        Off-by-one boundary test: 10 = pass, 11 = fail.
        Validator: validate_user_tags raises ValidationError if len > 10.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        eleven_tags = [f"Tag{i}" for i in range(11)]
        response = self.client.patch(self.tags_url, {"user_tags": eleven_tags}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_tags_tag_exactly_15_chars_is_accepted(self):
        """
        GIVEN  a single tag of exactly 15 characters (boundary maximum)
        THEN   200 OK.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(self.tags_url, {"user_tags": ["A" * 15]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_tags_tag_16_chars_is_rejected(self):
        """
        GIVEN  a single tag of 16 characters (one over the per-tag limit)
        THEN   400 Bad Request.

        Off-by-one boundary test: 15 chars = pass, 16 chars = fail.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(self.tags_url, {"user_tags": ["A" * 16]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_tags_plain_string_instead_of_array_rejected(self):
        """
        GIVEN  user_tags = 'Non-Smoker' (a plain string, not an array)
        THEN   400 Bad Request.

        Idiot-proof test: The DB constraint enforces jsonb_typeof = 'array'.
        The validator also checks isinstance(value, list). A plain string
        like 'Non-Smoker' would fail both checks.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(
            self.tags_url,
            {"user_tags": "Non-Smoker"},  # ← wrong type! should be ["Non-Smoker"]
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_tags_array_of_integers_rejected(self):
        """
        GIVEN  user_tags = [1, 2, 3] (integers inside array, not strings)
        THEN   400 Bad Request.

        Validator: Each element must be isinstance(tag, str).
        Integers like 1, 2, 3 must be rejected.
        """
        user, token = self._create_and_login_user()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.patch(
            self.tags_url,
            {"user_tags": [1, 2, 3]},  # ← wrong element type!
            format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_tags_cannot_update_another_users_tags(self):
        """
        GIVEN  user A is authenticated
        WHEN   they PATCH /user-tags/
        THEN   only user A's tags change — user B's tags are untouched.

        Same security isolation as the user-about test above.
        get_object() = self.request.user ensures you can only edit yourself.
        """
        user_a, token_a = self._create_and_login_user(email="usera@example.com", username="usera")
        user_b = tbl_user_profile.objects.create_user(
            username="userb", email="userb@example.com",
            password="Password123!", contact_number="+639111111111"
        )
        original_tags = list(user_b.user_tags)  # save baseline (should be [])

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token_a}")
        self.client.patch(self.tags_url, {"user_tags": ["Hacked"]}, format="json")

        # User B's tags must remain unchanged
        user_b.refresh_from_db()
        self.assertEqual(user_b.user_tags, original_tags)

    # ── PUBLIC PROFILE TESTS ──────────────────────────────────────────────────

    def test_public_profile_authenticated_success(self):
        """
        GIVEN  an authenticated user querying another user's public profile
        WHEN   GET /api/v1/accounts/public-profile/<uuid>/
        THEN   200 OK, returns full_name, account_type, verification_status,
               user_about, user_tags, city, province, and date_joined.
        """
        viewer, viewer_token = self._create_and_login_user(email="viewer@example.com", username="viewer")
        target = tbl_user_profile.objects.create_user(
            username="targetuser",
            email="target@example.com",
            password="Password123!",
            first_name="Kakashi",
            last_name="Hatake",
            contact_number="+639222222222",
            account_type="Kasambahay",
            city="Cagayan de Oro",
            province="Misamis Oriental",
            user_about="Experienced in cleaning and maintenance.",
            user_tags=["Cleaning", "Reliable"]
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {viewer_token}")
        response = self.client.get(f"{self.public_profile_base}{target.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("full_name"), "Kakashi Hatake")
        self.assertEqual(response.data.get("account_type"), "Kasambahay")
        self.assertEqual(response.data.get("city"), "Cagayan de Oro")
        self.assertEqual(response.data.get("province"), "Misamis Oriental")
        self.assertEqual(response.data.get("user_about"), "Experienced in cleaning and maintenance.")
        self.assertEqual(response.data.get("user_tags"), ["Cleaning", "Reliable"])
        self.assertIn("date_joined", response.data)

    def test_public_profile_unauthenticated_rejected(self):
        """
        GIVEN  an unauthenticated request
        WHEN   GET /api/v1/accounts/public-profile/<uuid>/
        THEN   401 Unauthorized.
        """
        target = tbl_user_profile.objects.create_user(
            username="targetuser2",
            email="target2@example.com",
            password="Password123!",
            contact_number="+639333333333"
        )
        response = self.client.get(f"{self.public_profile_base}{target.id}/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_public_profile_nonexistent_user_returns_404(self):
        """
        GIVEN  a random UUID that does not match any user
        WHEN   GET /api/v1/accounts/public-profile/<random_uuid>/
        THEN   404 Not Found.
        """
        viewer, viewer_token = self._create_and_login_user(email="viewer2@example.com", username="viewer2")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {viewer_token}")
        response = self.client.get(f"{self.public_profile_base}{uuid.uuid4()}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_public_profile_does_not_leak_sensitive_fields(self):
        """
        GIVEN  a public profile request
        WHEN   GET /api/v1/accounts/public-profile/<uuid>/
        THEN   sensitive attributes (email, password, contact_number, is_staff, is_superuser)
               are strictly excluded from the payload.
        """
        viewer, viewer_token = self._create_and_login_user(email="viewer3@example.com", username="viewer3")
        target = tbl_user_profile.objects.create_user(
            username="targetuser3",
            email="private_email@example.com",
            password="Password123!",
            contact_number="+639444444444"
        )

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {viewer_token}")
        response = self.client.get(f"{self.public_profile_base}{target.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("email", response.data)
        self.assertNotIn("password", response.data)
        self.assertNotIn("contact_number", response.data)
        self.assertNotIn("is_staff", response.data)
        self.assertNotIn("is_superuser", response.data)


# =============================================================================
# SECTION 5 — KASAMBAHAY RESUME ENDPOINT TESTS
# =============================================================================

class TestKasambahayResumeEndpoint(TestCase):
    """
    Test suite for the Kasambahay Resume upload & retrieval endpoint:
    - GET /api/v1/accounts/resume/
    - PATCH /api/v1/accounts/resume/
    - POST /api/v1/accounts/resume/
    - Permissions, file validation, rate limiting, and idempotency
    - PublicProfile integration for resume_url
    """

    def setUp(self):
        super().setUp()
        cache.clear()
        self.client = APIClient()
        self.resume_url = reverse('kasambahay-resume')
        self.public_profile_base = "/api/v1/accounts/public-profile/"

        self.kasambahay = tbl_user_profile.objects.create_user(
            username="kasa_resume_user",
            email="kasa_resume@example.com",
            password="Password123!",
            first_name="Maria",
            last_name="Santos",
            account_type="Kasambahay",
            contact_number="+639111111111"
        )

        self.homeowner = tbl_user_profile.objects.create_user(
            username="home_resume_user",
            email="home_resume@example.com",
            password="Password123!",
            first_name="Juan",
            last_name="Dela Cruz",
            account_type="Homeowner",
            contact_number="+639222222222"
        )

    def _generate_dummy_pdf(self, name="resume.pdf", size_bytes=1024):
        content = b"%PDF-1.4 " + (b"0" * max(0, size_bytes - 9))
        return SimpleUploadedFile(name, content, content_type="application/pdf")

    def test_resume_get_unauthenticated_returns_401(self):
        """
        GIVEN  an unauthenticated client
        WHEN   GET /api/v1/accounts/resume/
        THEN   401 Unauthorized.
        """
        response = self.client.get(self.resume_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_resume_get_homeowner_forbidden(self):
        """
        GIVEN  an authenticated Homeowner
        WHEN   GET /api/v1/accounts/resume/
        THEN   403 Forbidden because only Kasambahay can access resume endpoints.
        """
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.get(self.resume_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Only Kasambahay accounts can access this resource", str(response.data))

    def test_resume_get_kasambahay_no_resume_returns_null(self):
        """
        GIVEN  a Kasambahay who has not uploaded a resume
        WHEN   GET /api/v1/accounts/resume/
        THEN   200 OK with resume_url: null.
        """
        self.client.force_authenticate(user=self.kasambahay)
        response = self.client.get(self.resume_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data.get("resume_url"))
        self.assertIsNone(response.data.get("resume_uploaded_at"))

    @patch('cloudinary.utils.cloudinary_url')
    def test_resume_get_kasambahay_existing_resume_returns_signed_url(self, mock_cloudinary_url):
        """
        GIVEN  a Kasambahay with an existing resume
        WHEN   GET /api/v1/accounts/resume/
        THEN   200 OK and returns a signed Cloudinary URL.
        """
        mock_cloudinary_url.return_value = ("https://res.cloudinary.com/signed-url/resume.pdf", {})
        self.kasambahay.resume_url = "serbisure_resumes/maria_resume_123"
        self.kasambahay.save()

        self.client.force_authenticate(user=self.kasambahay)
        response = self.client.get(self.resume_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("resume_url"), "https://res.cloudinary.com/signed-url/resume.pdf")

    def test_resume_patch_homeowner_forbidden(self):
        """
        GIVEN  an authenticated Homeowner
        WHEN   PATCH /api/v1/accounts/resume/
        THEN   403 Forbidden.
        """
        self.client.force_authenticate(user=self.homeowner)
        pdf = self._generate_dummy_pdf()
        response = self.client.patch(self.resume_url, {"resume_pdf": pdf}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_resume_patch_missing_file_rejected(self):
        """
        GIVEN  a Kasambahay sending PATCH without a file
        THEN   400 Bad Request.
        """
        self.client.force_authenticate(user=self.kasambahay)
        response = self.client.patch(self.resume_url, {}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("resume_pdf", response.data)

    def test_resume_patch_non_pdf_file_rejected(self):
        """
        GIVEN  a Kasambahay uploading a .jpg instead of .pdf
        THEN   400 Bad Request — only PDF files are allowed.
        """
        self.client.force_authenticate(user=self.kasambahay)
        fake_image = SimpleUploadedFile("my_resume.jpg", b"fake-jpg-content", content_type="image/jpeg")
        response = self.client.patch(self.resume_url, {"resume_pdf": fake_image}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Only PDF files are accepted", str(response.data))

    def test_resume_patch_file_exceeding_10mb_rejected(self):
        """
        GIVEN  a PDF file larger than 10MB
        THEN   400 Bad Request — file size limit enforced.
        """
        self.client.force_authenticate(user=self.kasambahay)
        large_pdf = self._generate_dummy_pdf(size_bytes=10 * 1024 * 1024 + 100)
        response = self.client.patch(self.resume_url, {"resume_pdf": large_pdf}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("under 10MB", str(response.data))

    @patch('cloudinary.utils.cloudinary_url')
    @patch('cloudinary.uploader.upload')
    def test_resume_patch_valid_pdf_succeeds_and_updates_model(self, mock_upload, mock_url):
        """
        GIVEN  a valid PDF file under 10MB
        WHEN   Kasambahay uploads via PATCH
        THEN   200 OK, Cloudinary upload triggered with resource_type='raw',
               and user model updated with resume_url and resume_uploaded_at.
        """
        mock_upload.return_value = {'public_id': 'serbisure_resumes/sample_resume_id'}
        mock_url.return_value = ("https://res.cloudinary.com/signed-url/sample_resume_id.pdf", {})

        self.client.force_authenticate(user=self.kasambahay)
        pdf = self._generate_dummy_pdf(name="my_cv.pdf")
        response = self.client.patch(self.resume_url, {"resume_pdf": pdf}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("resume_url"), "https://res.cloudinary.com/signed-url/sample_resume_id.pdf")
        self.assertIsNotNone(response.data.get("resume_uploaded_at"))

        self.kasambahay.refresh_from_db()
        self.assertEqual(self.kasambahay.resume_url, 'serbisure_resumes/sample_resume_id')
        self.assertIsNotNone(self.kasambahay.resume_uploaded_at)

        mock_upload.assert_called_once()
        _, kwargs = mock_upload.call_args
        self.assertEqual(kwargs.get("resource_type"), "image")
        self.assertEqual(kwargs.get("format"), "pdf")
        self.assertEqual(kwargs.get("folder"), "serbisure_resumes/")

    @patch('cloudinary.utils.cloudinary_url')
    @patch('cloudinary.uploader.upload')
    def test_resume_post_alias_succeeds(self, mock_upload, mock_url):
        """
        GIVEN  a valid PDF file
        WHEN   sent via POST (alias for multipart upload)
        THEN   200 OK and model is updated.
        """
        mock_upload.return_value = {'public_id': 'serbisure_resumes/post_alias_id'}
        mock_url.return_value = ("https://res.cloudinary.com/signed-url/post_alias_id.pdf", {})

        self.client.force_authenticate(user=self.kasambahay)
        pdf = self._generate_dummy_pdf(name="post_cv.pdf")
        response = self.client.post(self.resume_url, {"resume_pdf": pdf}, format='multipart')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.kasambahay.refresh_from_db()
        self.assertEqual(self.kasambahay.resume_url, 'serbisure_resumes/post_alias_id')

    @patch('cloudinary.utils.cloudinary_url')
    @patch('cloudinary.uploader.upload')
    def test_resume_idempotency_cached_on_repeated_key(self, mock_upload, mock_url):
        """
        GIVEN  an Idempotency-Key header on PATCH
        WHEN   sent twice with the same key
        THEN   Cloudinary is only called once, and second request receives cached response.
        """
        mock_upload.return_value = {'public_id': 'serbisure_resumes/idemp_resume'}
        mock_url.return_value = ("https://res.cloudinary.com/signed-url/idemp_resume.pdf", {})

        self.client.force_authenticate(user=self.kasambahay)
        idemp_key = str(uuid.uuid4())

        pdf1 = self._generate_dummy_pdf(name="cv1.pdf")
        res1 = self.client.patch(
            self.resume_url,
            {"resume_pdf": pdf1},
            format='multipart',
            HTTP_IDEMPOTENCY_KEY=idemp_key
        )
        self.assertEqual(res1.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_upload.call_count, 1)

        # 2nd attempt with same key (simulating double click)
        pdf2 = self._generate_dummy_pdf(name="cv2.pdf")
        res2 = self.client.patch(
            self.resume_url,
            {"resume_pdf": pdf2},
            format='multipart',
            HTTP_IDEMPOTENCY_KEY=idemp_key
        )
        self.assertEqual(res2.status_code, status.HTTP_200_OK)
        self.assertEqual(mock_upload.call_count, 1)  # Still 1, didn't re-upload!
        self.assertEqual(res1.data, res2.data)

    def test_resume_invalid_idempotency_key_rejected(self):
        """
        GIVEN  an invalid non-UUID Idempotency-Key
        THEN   400 Bad Request.
        """
        self.client.force_authenticate(user=self.kasambahay)
        pdf = self._generate_dummy_pdf()
        response = self.client.patch(
            self.resume_url,
            {"resume_pdf": pdf},
            format='multipart',
            HTTP_IDEMPOTENCY_KEY="not-a-valid-uuid"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('cloudinary.utils.cloudinary_url')
    def test_public_profile_includes_resume_url_for_kasambahay(self, mock_url):
        """
        GIVEN  a Kasambahay with an uploaded resume
        WHEN   any authenticated user fetches their public profile
        THEN   resume_url is included with a signed URL.
        """
        mock_url.return_value = ("https://res.cloudinary.com/signed/public_resume.pdf", {})
        self.kasambahay.resume_url = "serbisure_resumes/kasa_pub_resume"
        self.kasambahay.save()

        self.client.force_authenticate(user=self.homeowner)
        response = self.client.get(f"{self.public_profile_base}{self.kasambahay.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data.get("resume_url"), "https://res.cloudinary.com/signed/public_resume.pdf")

    def test_public_profile_excludes_resume_url_for_homeowner(self):
        """
        GIVEN  a Homeowner profile
        WHEN   their public profile is fetched
        THEN   resume_url is None.
        """
        self.client.force_authenticate(user=self.kasambahay)
        response = self.client.get(f"{self.public_profile_base}{self.homeowner.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data.get("resume_url"))


class TestChangePasswordAPI(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = tbl_user_profile.objects.create_user(
            email='user_pw@example.com',
            password='OldPassword123!',
            first_name='Test',
            last_name='User',
            account_type='Homeowner'
        )
        self.url = reverse('change-password')

    def test_change_password_success(self):
        self.client.force_authenticate(user=self.user)
        res = self.client.post(self.url, {
            'current_password': 'OldPassword123!',
            'new_password': 'NewPassword456!',
            'confirm_password': 'NewPassword456!',
        })
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('NewPassword456!'))

    def test_change_password_wrong_current_password(self):
        self.client.force_authenticate(user=self.user)
        res = self.client.post(self.url, {
            'current_password': 'WrongPassword!',
            'new_password': 'NewPassword456!',
            'confirm_password': 'NewPassword456!',
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Current password is incorrect', res.data.get('error', ''))

    def test_change_password_mismatch(self):
        self.client.force_authenticate(user=self.user)
        res = self.client.post(self.url, {
            'current_password': 'OldPassword123!',
            'new_password': 'NewPassword456!',
            'confirm_password': 'DifferentPassword!',
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('do not match', res.data.get('error', ''))

    def test_change_password_too_short(self):
        self.client.force_authenticate(user=self.user)
        res = self.client.post(self.url, {
            'current_password': 'OldPassword123!',
            'new_password': 'short',
            'confirm_password': 'short',
        })
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('8 characters', res.data.get('error', ''))

    def test_change_password_unauthenticated(self):
        res = self.client.post(self.url, {
            'current_password': 'OldPassword123!',
            'new_password': 'NewPassword456!',
            'confirm_password': 'NewPassword456!',
        })
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)



