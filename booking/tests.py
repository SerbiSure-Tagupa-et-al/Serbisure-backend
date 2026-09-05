from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
import datetime
from .models import tbl_booking
import uuid
from django.core.cache import cache

User = get_user_model()

class BookingTests(APITestCase):

    def setUp(self):
        # Create a verified Homeowner
        self.verified_user = User.objects.create_user(
            email='homeowner@test.com',
            password='TestPassword123!',
            username='homeowner1',
            first_name='Home',
            last_name='Owner',
            account_type='Homeowner',
            verification_status='Verified'
        )

        # Create an unverified user
        self.unverified_user = User.objects.create_user(
            email='unverified@test.com',
            password='TestPassword123!',
            username='unverified1',
            first_name='Unverified',
            last_name='User',
            account_type='Homeowner',
            verification_status='Unverified'
        )

        self.url = reverse('booking-post') # Grabs the URL automatically!
        self.valid_uuid = str(uuid.uuid4())

        # Set up safe time-travel dates for testing
        self.future_start = timezone.now() + datetime.timedelta(days=1)
        self.future_end = timezone.now() + datetime.timedelta(days=2)
        self.past_time = timezone.now() - datetime.timedelta(days=1)

    def tearDown(self):
        # Clear the cache after every single test so the idempotency keys reset
        cache.clear()

    def get_valid_payload(self):
        return {
            "booking_type": "short_term",
            "service_category": ["Cleaning"],
            "service_address": "123 Test St",
            "zip_code": "9000",
            "daily_rate": "500.00",
            "special_instruction": "Please be careful with the vase.",
            "start_time": self.future_start.isoformat(),
            "end_time": self.future_end.isoformat()
        }

    # --- HAPPY PATH ---
    def test_create_booking_success(self):
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        response = self.client.post(self.url, self.get_valid_payload(), format='json', **headers)
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(tbl_booking.objects.count(), 1)
        # Ensure it correctly auto-assigned the user!
        self.assertEqual(tbl_booking.objects.first().poster_id, self.verified_user)
        # Ensure it defaulted to Pending!
        self.assertEqual(tbl_booking.objects.first().booking_status, 'Pending')

    # --- IDEMPOTENCY (ANTI-SPAM) TESTS ---
    def test_missing_idempotency_key(self):
        self.client.force_authenticate(user=self.verified_user)
        
        response = self.client.post(self.url, self.get_valid_payload(), format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Idempotency-Key", str(response.data))

    def test_invalid_idempotency_key(self):
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': 'not-a-uuid'}
        
        response = self.client.post(self.url, self.get_valid_payload(), format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_idempotency_caching_blocks_duplicates(self):
        """Test if submitting the same UUID twice prevents duplicate database entries"""
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        # Request 1 (Succeeds and saves to cache)
        res1 = self.client.post(self.url, self.get_valid_payload(), format='json', **headers)
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        
        # Request 2 (Returns cached response, database does NOT increment!)
        res2 = self.client.post(self.url, self.get_valid_payload(), format='json', **headers)
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(tbl_booking.objects.count(), 1) # Still 1! The anti-spam worked perfectly!

    # --- SECURITY & AUTHENTICATION TESTS ---
    def test_unauthenticated_user_blocked(self):
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        response = self.client.post(self.url, self.get_valid_payload(), format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unverified_user_blocked(self):
        self.client.force_authenticate(user=self.unverified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        response = self.client.post(self.url, self.get_valid_payload(), format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # --- THE "IDIOT" CASES (VALIDATION ERRORS) ---
    def test_idiot_time_travel_end_before_start(self):
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        payload = self.get_valid_payload()
        # Idiot sets End time BEFORE start time!
        payload['start_time'] = self.future_end.isoformat()
        payload['end_time'] = self.future_start.isoformat()

        response = self.client.post(self.url, payload, format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("end_time", response.data) # Make sure the error message points to end_time

    def test_idiot_start_time_in_the_past(self):
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        payload = self.get_valid_payload()
        payload['start_time'] = self.past_time.isoformat()

        response = self.client.post(self.url, payload, format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("start_time", response.data)

    def test_idiot_tries_to_hack_booking_status(self):
        """Test to make sure a user cannot force a job to be 'Completed' upon creation"""
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        payload = self.get_valid_payload()
        payload['booking_status'] = 'Completed' # Trying to hack the system
        
        response = self.client.post(self.url, payload, format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        # Check database: Read_only_fields should have ignored 'Completed' and forced it to 'Pending'!
        booking = tbl_booking.objects.first()
        self.assertEqual(booking.booking_status, 'Pending')

    def test_idiot_types_wrong_booking_type(self):
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        payload = self.get_valid_payload()
        payload['booking_type'] = 'super_long_term' # Invalid choice
        
        response = self.client.post(self.url, payload, format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class BookingFeedFilterTests(APITestCase):

    def setUp(self):
        self.homeowner = User.objects.create_user(
            email='homeowner_feed@test.com',
            password='TestPassword123!',
            username='homeowner_feed',
            first_name='Maria',
            last_name='Santos',
            account_type='Homeowner',
            verification_status='Verified'
        )

        self.kasambahay = User.objects.create_user(
            email='kasambahay_feed@test.com',
            password='TestPassword123!',
            username='kasambahay_feed',
            first_name='Ana',
            last_name='Reyes',
            account_type='Kasambahay',
            verification_status='Verified'
        )

        self.feed_url = reverse('booking-feed')
        now = timezone.now()

        # Create bookings posted by Kasambahay (visible to Homeowner)
        self.k_job1 = tbl_booking.objects.create(
            poster_id=self.kasambahay,
            booking_type='short_term',
            booking_status='Pending',
            service_category=['Cleaning'],
            start_time=now + datetime.timedelta(days=1),
            service_address='Barangay Carmen, Cagayan de Oro',
            daily_rate=500.00
        )

        self.k_job2 = tbl_booking.objects.create(
            poster_id=self.kasambahay,
            booking_type='long_term',
            booking_status='Pending',
            service_category=['Cooking', 'Caregiver'],
            start_time=now + datetime.timedelta(days=2),
            service_address='Nazareth, Cagayan de Oro',
            daily_rate=1200.00
        )

    def tearDown(self):
        cache.clear()

    def test_feed_category_filter(self):
        self.client.force_authenticate(user=self.homeowner)
        res = self.client.get(f"{self.feed_url}?category=Cleaning")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['booking_id'], str(self.k_job1.booking_id))

    def test_feed_booking_type_filter(self):
        self.client.force_authenticate(user=self.homeowner)
        res = self.client.get(f"{self.feed_url}?booking_type=long_term")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['booking_id'], str(self.k_job2.booking_id))

    def test_feed_max_rate_filter(self):
        self.client.force_authenticate(user=self.homeowner)
        res = self.client.get(f"{self.feed_url}?max_rate=600")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['booking_id'], str(self.k_job1.booking_id))

    def test_feed_location_filter(self):
        self.client.force_authenticate(user=self.homeowner)
        res = self.client.get(f"{self.feed_url}?location=Nazareth")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['booking_id'], str(self.k_job2.booking_id))

    def test_feed_sort_rate(self):
        self.client.force_authenticate(user=self.homeowner)
        res = self.client.get(f"{self.feed_url}?sort=rate_asc")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 2)
        self.assertEqual(float(res.data[0]['daily_rate']), 500.00)
        self.assertEqual(float(res.data[1]['daily_rate']), 1200.00)

