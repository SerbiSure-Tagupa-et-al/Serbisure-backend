from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.core.cache import cache
import datetime
import uuid
from decimal import Decimal

from booking.models import tbl_booking, tbl_booking_assignment
from .models import tbl_review

User = get_user_model()


class ReviewTests(APITestCase):

    def setUp(self):
        # 1. Verified Homeowner (Poster)
        self.homeowner = User.objects.create_user(
            email='homeowner@test.com',
            password='TestPassword123!',
            username='homeowner1',
            first_name='Juan',
            last_name='Dela Cruz',
            account_type='Homeowner',
            verification_status='Verified',
            contact_number='+639123456781'
        )

        # 2. Verified Kasambahay (Worker/Accepter)
        self.kasambahay = User.objects.create_user(
            email='kasambahay@test.com',
            password='TestPassword123!',
            username='kasambahay1',
            first_name='Maria',
            last_name='Santos',
            account_type='Kasambahay',
            verification_status='Verified',
            contact_number='+639123456782'
        )

        # 3. Third Verified User (Not part of any booking = bystander)
        self.bystander = User.objects.create_user(
            email='bystander@test.com',
            password='TestPassword123!',
            username='bystander1',
            first_name='Pedro',
            last_name='Penduko',
            account_type='Homeowner',
            verification_status='Verified',
            contact_number='+639123456783'
        )

        # 4. Unverified User
        self.unverified_user = User.objects.create_user(
            email='unverified@test.com',
            password='TestPassword123!',
            username='unverified1',
            first_name='Ana',
            last_name='Reyes',
            account_type='Kasambahay',
            verification_status='Unverified',
            contact_number='+639123456784'
        )

        # 5. Completed Booking (Homeowner posted, Kasambahay accepted)
        self.completed_booking = tbl_booking.objects.create(
            poster_id=self.homeowner,
            booking_type='short_term',
            booking_status='Completed',
            service_category=['Cleaning'],
            start_time=timezone.now() - datetime.timedelta(days=2),
            end_time=timezone.now() - datetime.timedelta(days=1),
            service_address='123 Main St, CDO',
            zip_code='9000',
            daily_rate=Decimal('500.00')
        )

        # Assign Kasambahay to completed booking
        self.assignment = tbl_booking_assignment.objects.create(
            booking_id=self.completed_booking,
            accepter_id=self.kasambahay
        )

        # 6. Pending Booking (Not Completed — cannot review)
        self.pending_booking = tbl_booking.objects.create(
            poster_id=self.homeowner,
            booking_type='short_term',
            booking_status='Pending',
            service_category=['Cleaning'],
            start_time=timezone.now() + datetime.timedelta(days=1),
            end_time=timezone.now() + datetime.timedelta(days=2),
            service_address='123 Main St, CDO',
            zip_code='9000',
            daily_rate=Decimal('500.00')
        )

        self.create_url = reverse('review-create')
        self.received_url = reverse('review-received')
        self.given_url = reverse('review-given')

    def tearDown(self):
        cache.clear()

    # ─────────────────────────────────────────────────────────────────────
    # SECURITY & AUTH GUARDS
    # ─────────────────────────────────────────────────────────────────────

    def test_unauthenticated_cannot_create_review(self):
        """No token at all — must get 401."""
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Great service indeed!",
            "nlp_sentiment": "Positive"
        })
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unauthenticated_cannot_read_reviews(self):
        """GET endpoints must also be protected — no free browsing."""
        self.assertEqual(self.client.get(self.received_url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.get(self.given_url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unverified_user_cannot_create_review(self):
        """Unverified user must get 403 Forbidden."""
        self.client.force_authenticate(user=self.unverified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Great service indeed!",
            "nlp_sentiment": "Positive"
        }, **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("Only verified users can submit reviews", response.data['detail'])

    # ─────────────────────────────────────────────────────────────────────
    # IDEMPOTENCY KEY VALIDATION
    # ─────────────────────────────────────────────────────────────────────

    def test_missing_idempotency_key_returns_400(self):
        """No Idempotency-Key header at all — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Great service indeed!",
            "nlp_sentiment": "Positive"
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Idempotency-Key header is required", response.data['detail'])

    def test_non_uuid_idempotency_key_returns_400(self):
        """Plain string idempotency key — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        headers = {'HTTP_IDEMPOTENCY_KEY': 'i-am-not-a-uuid'}
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Great service indeed!",
            "nlp_sentiment": "Positive"
        }, **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_string_idempotency_key_returns_400(self):
        """Empty string idempotency key (idiot user: sends key but leaves it blank)."""
        self.client.force_authenticate(user=self.homeowner)
        headers = {'HTTP_IDEMPOTENCY_KEY': ''}
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Great service indeed!",
            "nlp_sentiment": "Positive"
        }, **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ─────────────────────────────────────────────────────────────────────
    # BUSINESS LOGIC & DOMAIN VALIDATION
    # ─────────────────────────────────────────────────────────────────────

    def test_review_non_completed_booking_fails(self):
        """Cannot review a Pending booking — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
        response = self.client.post(self.create_url, {
            "booking_id": str(self.pending_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Great service indeed!",
            "nlp_sentiment": "Positive"
        }, **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("booking_id", response.data)

    def test_non_participant_bystander_cannot_review(self):
        """Verified user who was NOT part of the booking cannot review."""
        self.client.force_authenticate(user=self.bystander)
        headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "I was not there at all!",
            "nlp_sentiment": "Neutral"
        }, **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not an authorized participant", str(response.data['detail']))

    def test_fake_booking_uuid_returns_400(self):
        """Idiot user submits a valid-looking UUID but for a booking that doesn't exist."""
        self.client.force_authenticate(user=self.homeowner)
        headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
        response = self.client.post(self.create_url, {
            "booking_id": str(uuid.uuid4()),  # Random UUID — not in DB
            "rating": 5,
            "unstructured_feedback": "Great service indeed!",
            "nlp_sentiment": "Positive"
        }, **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_nlp_sentiment_rejected(self):
        """User tries to send an arbitrary string as nlp_sentiment."""
        self.client.force_authenticate(user=self.homeowner)
        headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Great service indeed!",
            "nlp_sentiment": "HappyVibes"  # Invalid — not in choices
        }, **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("nlp_sentiment", response.data)

    # ─────────────────────────────────────────────────────────────────────
    # HAPPY PATHS
    # ─────────────────────────────────────────────────────────────────────

    def test_poster_reviews_accepter_success(self):
        """Homeowner (poster) reviews Kasambahay (accepter) — golden path."""
        self.client.force_authenticate(user=self.homeowner)
        idempotency_key = str(uuid.uuid4())
        headers = {'HTTP_IDEMPOTENCY_KEY': idempotency_key}

        payload = {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Outstanding cleaning work! Very punctual.",
            "nlp_sentiment": "Positive"
        }
        response = self.client.post(self.create_url, payload, **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(tbl_review.objects.count(), 1)

        review = tbl_review.objects.first()
        self.assertEqual(review.reviewer_id, self.homeowner)
        self.assertEqual(review.reviewee_id, self.kasambahay)
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.nlp_sentiment, "Positive")

    def test_accepter_reviews_poster_success(self):
        """Kasambahay (accepter) reviews Homeowner (poster) — golden path."""
        self.client.force_authenticate(user=self.kasambahay)
        headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}

        payload = {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 4,
            "unstructured_feedback": "Polite employer and clear instructions.",
            "nlp_sentiment": "Positive"
        }
        response = self.client.post(self.create_url, payload, **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        review = tbl_review.objects.first()
        self.assertEqual(review.reviewer_id, self.kasambahay)
        self.assertEqual(review.reviewee_id, self.homeowner)
        self.assertEqual(review.rating, 4)

    # ─────────────────────────────────────────────────────────────────────
    # IDEMPOTENCY CACHE BEHAVIOR
    # ─────────────────────────────────────────────────────────────────────

    def test_idempotency_double_click_safe(self):
        """Same idempotency key submitted twice — only creates ONE review."""
        self.client.force_authenticate(user=self.homeowner)
        idempotency_key = str(uuid.uuid4())
        headers = {'HTTP_IDEMPOTENCY_KEY': idempotency_key}

        payload = {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "Outstanding cleaning work! Very punctual.",
            "nlp_sentiment": "Positive"
        }
        r1 = self.client.post(self.create_url, payload, **headers)
        r2 = self.client.post(self.create_url, payload, **headers)

        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(tbl_review.objects.count(), 1)  # Only one saved!

    def test_different_idempotency_key_is_blocked_by_db_duplicate_check(self):
        """User tries a second review with a fresh idempotency key — should be caught by app logic."""
        self.client.force_authenticate(user=self.homeowner)

        self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "First attempt feedback here.",
            "nlp_sentiment": "Positive"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})

        # Second attempt with a DIFFERENT key (genuine second try, not a network retry)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 3,
            "unstructured_feedback": "Second attempt feedback here.",
            "nlp_sentiment": "Neutral"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already submitted a review", str(response.data['detail']))
        self.assertEqual(tbl_review.objects.count(), 1)

    # ─────────────────────────────────────────────────────────────────────
    # RATING BOUNDARY TESTS
    # ─────────────────────────────────────────────────────────────────────

    def test_rating_zero_rejected(self):
        """Rating of 0 is below minimum — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 0,
            "unstructured_feedback": "Some valid feedback here.",
            "nlp_sentiment": "Negative"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("rating", response.data)

    def test_rating_six_rejected(self):
        """Rating of 6 is above maximum — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 6,
            "unstructured_feedback": "Some valid feedback here.",
            "nlp_sentiment": "Positive"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("rating", response.data)

    def test_rating_negative_rejected(self):
        """Rating of -1 (idiot user sends negative) — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": -1,
            "unstructured_feedback": "Some valid feedback here.",
            "nlp_sentiment": "Negative"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rating_string_rejected(self):
        """Rating sent as a string 'five' instead of integer 5 — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": "five",
            "unstructured_feedback": "Some valid feedback here.",
            "nlp_sentiment": "Positive"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rating_boundary_min_1_accepted(self):
        """Rating of exactly 1 must be valid."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 1,
            "unstructured_feedback": "Really bad experience here.",
            "nlp_sentiment": "Negative"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # ─────────────────────────────────────────────────────────────────────
    # FEEDBACK VALIDATION
    # ─────────────────────────────────────────────────────────────────────

    def test_feedback_too_short_rejected(self):
        """Feedback shorter than 10 chars must be rejected."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "ok",  # Only 2 chars
            "nlp_sentiment": "Positive"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("unstructured_feedback", response.data)

    def test_feedback_empty_rejected(self):
        """Empty feedback must be rejected (allow_blank=False)."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "",
            "nlp_sentiment": "Positive"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_feedback_missing_field_rejected(self):
        """No feedback field at all (idiot user forgets it) — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "nlp_sentiment": "Positive"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("unstructured_feedback", response.data)

    def test_feedback_at_max_1000_chars_accepted(self):
        """Feedback at exactly 1000 characters is valid."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "x" * 1000,
            "nlp_sentiment": "Positive"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_feedback_over_1000_chars_rejected(self):
        """Feedback over 1000 characters — must get 400."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.post(self.create_url, {
            "booking_id": str(self.completed_booking.booking_id),
            "rating": 5,
            "unstructured_feedback": "x" * 1001,
            "nlp_sentiment": "Positive"
        }, **{'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ─────────────────────────────────────────────────────────────────────
    # LIST & SUMMARY ENDPOINTS
    # ─────────────────────────────────────────────────────────────────────

    def test_received_reviews_returns_empty_list_not_500(self):
        """User with zero reviews gets [] not a 500 error."""
        self.client.force_authenticate(user=self.kasambahay)
        response = self.client.get(self.received_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_given_reviews_returns_empty_list_not_500(self):
        """User who never left a review gets [] not a 500 error."""
        self.client.force_authenticate(user=self.homeowner)
        response = self.client.get(self.given_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_get_received_and_given_reviews(self):
        """Full round-trip: create review, then verify in received and given lists."""
        tbl_review.objects.create(
            booking_id=self.completed_booking,
            reviewer_id=self.homeowner,
            reviewee_id=self.kasambahay,
            rating=5,
            unstructured_feedback="Excellent job done well!",
            nlp_sentiment="Positive"
        )

        self.client.force_authenticate(user=self.kasambahay)
        rec_res = self.client.get(self.received_url)
        self.assertEqual(rec_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(rec_res.data), 1)
        self.assertEqual(rec_res.data[0]['rating'], 5)

        self.client.force_authenticate(user=self.homeowner)
        giv_res = self.client.get(self.given_url)
        self.assertEqual(giv_res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(giv_res.data), 1)
        self.assertEqual(giv_res.data[0]['reviewer_name'], "Juan Dela Cruz")

    def test_get_review_summary_correct_aggregation(self):
        """Summary endpoint returns correct avg, count, breakdown."""
        tbl_review.objects.create(
            booking_id=self.completed_booking,
            reviewer_id=self.homeowner,
            reviewee_id=self.kasambahay,
            rating=5,
            unstructured_feedback="Top tier service amazing!",
            nlp_sentiment="Positive"
        )

        self.client.force_authenticate(user=self.homeowner)
        summary_url = reverse('review-summary', kwargs={'user_id': self.kasambahay.id})
        response = self.client.get(summary_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data['data']
        self.assertEqual(data['average_rating'], 5.0)
        self.assertEqual(data['total_reviews'], 1)
        self.assertEqual(data['sentiment_breakdown']['Positive'], 1)
        self.assertEqual(data['sentiment_breakdown']['Neutral'], 0)
        self.assertEqual(data['sentiment_breakdown']['Negative'], 0)
        self.assertEqual(data['rating_breakdown']['5'], 1)
        self.assertEqual(data['rating_breakdown']['1'], 0)

    def test_get_review_summary_zero_reviews(self):
        """Summary for user with zero reviews returns avg=0.0 and all counts=0."""
        self.client.force_authenticate(user=self.homeowner)
        summary_url = reverse('review-summary', kwargs={'user_id': self.kasambahay.id})
        response = self.client.get(summary_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data['data']
        self.assertEqual(data['average_rating'], 0.0)
        self.assertEqual(data['total_reviews'], 0)

    def test_get_review_summary_nonexistent_user_returns_404(self):
        """Summary for a random UUID that doesn't belong to any user."""
        self.client.force_authenticate(user=self.homeowner)
        fake_id = uuid.uuid4()
        summary_url = reverse('review-summary', kwargs={'user_id': fake_id})
        response = self.client.get(summary_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("User not found", response.data['detail'])

    def test_get_user_reviews_list_for_target(self):
        """Public review list for a target user returns their reviews."""
        tbl_review.objects.create(
            booking_id=self.completed_booking,
            reviewer_id=self.homeowner,
            reviewee_id=self.kasambahay,
            rating=5,
            unstructured_feedback="Excellent work done here!",
            nlp_sentiment="Positive"
        )

        self.client.force_authenticate(user=self.bystander)
        user_reviews_url = reverse('review-user-list', kwargs={'user_id': self.kasambahay.id})
        response = self.client.get(user_reviews_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['rating'], 5)

    def test_get_user_reviews_nonexistent_user_returns_404(self):
        """UserReviewsView for a phantom UUID returns 404, not an empty list."""
        self.client.force_authenticate(user=self.homeowner)
        user_reviews_url = reverse('review-user-list', kwargs={'user_id': uuid.uuid4()})
        response = self.client.get(user_reviews_url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn("User not found", response.data['detail'])
