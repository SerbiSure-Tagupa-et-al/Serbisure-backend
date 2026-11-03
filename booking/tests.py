from accounts.testing import create_verified_test_user
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
        self.verified_user = create_verified_test_user(
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
            "booking_type": "short_term", 'work_setup': 'stay_out', 'rate_period': 'day',
            "service_category": ["Cleaning"],
            "region": "Region X - Northern Mindanao",
            "province": "Misamis Oriental",
            "city": "Cagayan de Oro",
            "barangay": "Macasandig",
            "street": "123 Test St",
            "zip_code": "9000",
            "rate_amount": "800.00",
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
    def test_legacy_public_date_range_is_ignored(self):
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        payload = self.get_valid_payload()
        # Old APK calendar terms no longer schedule a public advertisement.
        payload['start_time'] = self.future_end.isoformat()
        payload['end_time'] = self.future_start.isoformat()

        response = self.client.post(self.url, payload, format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data['data']['end_time'])
        self.assertEqual(response.data['data']['work_days'], [])

    def test_legacy_public_past_date_is_ignored(self):
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': self.valid_uuid}
        
        payload = self.get_valid_payload()
        payload['start_time'] = self.past_time.isoformat()

        response = self.client.post(self.url, payload, format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(response.data['data']['start_time'], self.past_time.isoformat())
        self.assertEqual(tbl_booking.objects.get().start_time.astimezone(timezone.get_current_timezone()).date(), timezone.localdate())

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

    # --- BATAS KASAMBAHAY STATUTORY MINIMUM WAGE TESTS ---
    def test_long_term_booking_below_minimum_wage_rejected(self):
        """
        GIVEN a long_term booking in Cagayan de Oro (Region X)
        WHEN rate_amount is below statutory minimum wage (₱6,500.00 / month)
        THEN serializer must reject with HTTP 400 mentioning Batas Kasambahay (RA 10361).
        """
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
        payload = self.get_valid_payload()
        payload['booking_type'] = 'long_term'
        payload['rate_period'] = 'month'
        payload['region'] = 'Region X - Northern Mindanao'
        payload['province'] = 'Misamis Oriental'
        payload['city'] = 'Cagayan de Oro'
        payload['barangay'] = 'Macasandig'
        payload['street'] = '123 Test St'
        payload['rate_amount'] = '6499.00'  # Below the monthly Region X floor

        response = self.client.post(self.url, payload, format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("rate_amount", response.data)
        self.assertIn("RBX-DW-06", str(response.data["rate_amount"]))

    def test_long_term_booking_at_minimum_wage_accepted(self):
        """
        GIVEN a long_term booking in Cagayan de Oro (Region X)
        WHEN rate_amount meets statutory minimum wage (₱6,500.00 / month)
        THEN booking creation must succeed with HTTP 201.
        """
        self.client.force_authenticate(user=self.verified_user)
        headers = {'HTTP_IDEMPOTENCY_KEY': str(uuid.uuid4())}
        payload = self.get_valid_payload()
        payload['booking_type'] = 'long_term'
        payload['rate_period'] = 'month'
        payload['region'] = 'Region X - Northern Mindanao'
        payload['province'] = 'Misamis Oriental'
        payload['city'] = 'Cagayan de Oro'
        payload['barangay'] = 'Macasandig'
        payload['street'] = '123 Test St'
        payload['rate_amount'] = '6500.00'

        response = self.client.post(self.url, payload, format='json', **headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_short_term_booking_below_platform_minimum_rejected(self):
        self.client.force_authenticate(user=self.verified_user)
        payload = self.get_valid_payload()
        payload['rate_amount'] = '599.00'
        response = self.client.post(self.url, payload, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('rate_amount', response.data)


class BookingFeedFilterTests(APITestCase):

    def setUp(self):
        self.homeowner = create_verified_test_user(
            email='homeowner_feed@test.com',
            password='TestPassword123!',
            username='homeowner_feed',
            first_name='Maria',
            last_name='Santos',
            account_type='Homeowner',
            verification_status='Verified'
        )

        self.kasambahay = create_verified_test_user(
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
            booking_type='short_term', rate_period='day',
            booking_status='Pending',
            service_category=['Cleaning'],
            start_time=now + datetime.timedelta(days=1),
            region='Region X - Northern Mindanao',
            province='Misamis Oriental',
            city='Cagayan de Oro',
            barangay='Carmen',
            street='Barangay Carmen',
            rate_amount=800.00
        )

        self.k_job2 = tbl_booking.objects.create(
            poster_id=self.kasambahay,
            booking_type='long_term', rate_period='month',
            booking_status='Pending',
            service_category=['Cooking', 'Caregiver'],
            start_time=now + datetime.timedelta(days=2),
            region='Region X - Northern Mindanao',
            province='Misamis Oriental',
            city='Cagayan de Oro',
            barangay='Nazareth',
            street='Nazareth',
            rate_amount=12000.00
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
        res = self.client.get(f"{self.feed_url}?max_rate=1000&rate_period=day")
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
        res = self.client.get(f"{self.feed_url}?sort=rate_asc&rate_period=day")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(float(res.data[0]['rate_amount']), 800.00)
        monthly = self.client.get(f"{self.feed_url}?sort=rate_asc&rate_period=month")
        self.assertEqual(len(monthly.data), 1)
        self.assertEqual(float(monthly.data[0]['rate_amount']), 12000.00)


class BookingLifecycleAndProposalTests(APITestCase):
    def setUp(self):
        now = timezone.now()
        self.homeowner = create_verified_test_user(
            email='lifecycle_h@test.com',
            password='TestPassword123!',
            username='lifecycle_h',
            first_name='Maria',
            last_name='Clara',
            account_type='Homeowner',
            verification_status='Verified',
            city='Cagayan de Oro'
        )

        self.kasambahay = create_verified_test_user(
            email='lifecycle_k@test.com',
            password='TestPassword123!',
            username='lifecycle_k',
            first_name='Juana',
            last_name='Dela Cruz',
            account_type='Kasambahay',
            verification_status='Verified',
            city='Cagayan de Oro',
            user_tags=['Cleaning', 'Cooking']
        )

        self.booking = tbl_booking.objects.create(
            poster_id=self.homeowner,
            booking_type='short_term', rate_period='day',
            booking_status='Pending',
            service_category=['Cleaning'],
            start_time=now + datetime.timedelta(days=1),
            region='Region X - Northern Mindanao',
            province='Misamis Oriental',
            city='Cagayan de Oro',
            barangay='Carmen',
            street='Barangay Carmen',
            rate_amount=800.00
        )

    def tearDown(self):
        cache.clear()

    def test_accept_booking(self):
        self.client.force_authenticate(user=self.kasambahay)
        url = reverse('booking-accept', kwargs={'booking_id': self.booking.booking_id})
        res = self.client.patch(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.booking_status, 'Accepted')
        self.assertEqual(self.booking.assignments.first().accepter_id, self.kasambahay)

    def test_start_and_complete_booking(self):
        self.client.force_authenticate(user=self.kasambahay)
        # 1. Accept
        self.client.patch(reverse('booking-accept', kwargs={'booking_id': self.booking.booking_id}))
        # Simulate the agreed work time arriving after acceptance.
        self.booking.start_time = timezone.now() - datetime.timedelta(minutes=1)
        self.booking.save(update_fields=['start_time'])
        # 2. Start
        start_res = self.client.patch(reverse('booking-start', kwargs={'booking_id': self.booking.booking_id}))
        self.assertEqual(start_res.status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.booking_status, 'InProgress')
        # 3. Complete
        complete_res = self.client.patch(reverse('booking-complete', kwargs={'booking_id': self.booking.booking_id}))
        self.assertEqual(complete_res.status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.booking_status, 'Completed')

    def test_cancel_booking(self):
        self.client.force_authenticate(user=self.homeowner)
        url = reverse('booking-cancel', kwargs={'booking_id': self.booking.booking_id})
        res = self.client.patch(url, {'reason': 'Change of household plans'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.booking_status, 'Cancelled')

    def test_my_bookings_list(self):
        self.client.force_authenticate(user=self.homeowner)
        res = self.client.get(reverse('booking-mine'))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 1)

    def test_proposal_flow(self):
        self.client.force_authenticate(user=self.kasambahay)
        # 1. Submit proposal
        prop_url = reverse('booking-proposals-create', kwargs={'booking_id': self.booking.booking_id})
        res = self.client.post(prop_url, {'proposed_rate': '650.00', 'rate_period': 'day', 'message': 'Can I request 650 for travel?'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        prop_id = res.data['proposal']['proposal_id']

        # 2. Homeowner responds & accepts
        self.client.force_authenticate(user=self.homeowner)
        respond_url = reverse('booking-proposals-respond', kwargs={'proposal_id': prop_id})
        resp = self.client.patch(respond_url, {'action': 'accept'}, format='json')
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.booking_status, 'Accepted')
        self.assertEqual(float(self.booking.rate_amount), 650.00)

    def test_recommendations(self):
        self.client.force_authenticate(user=self.kasambahay)
        res = self.client.get(reverse('booking-recommendations'))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn('recommendations', res.data)

    def test_long_term_proposal_below_minimum_wage_rejected(self):
        """
        GIVEN an existing long_term booking in Cagayan de Oro (minimum ₱6,500.00/month)
        WHEN a proposal offers below ₱250.00 (e.g. ₱180.00)
        THEN proposal must be rejected with HTTP 400 citing Batas Kasambahay minimum wage.
        """
        now = timezone.now()
        long_booking = tbl_booking.objects.create(
            poster_id=self.homeowner,
            booking_type='long_term', rate_period='month',
            booking_status='Pending',
            service_category=['Cleaning'],
            start_time=now + datetime.timedelta(days=1),
            region='Region X - Northern Mindanao',
            province='Misamis Oriental',
            city='Cagayan de Oro',
            barangay='Macasandig',
            street='Macasandig',
            rate_amount=6500.00
        )
        self.client.force_authenticate(user=self.kasambahay)
        prop_url = reverse('booking-proposals-create', kwargs={'booking_id': long_booking.booking_id})
        res = self.client.post(prop_url, {'proposed_rate': '6499.00', 'rate_period': 'month', 'message': 'Discounted rate'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('proposed_rate', res.data)

    def test_long_term_proposal_at_or_above_minimum_wage_accepted(self):
        """
        GIVEN an existing long_term booking
        WHEN a proposal offers at or above statutory minimum wage (₱6,500.00/month)
        THEN proposal creation succeeds with HTTP 201.
        """
        now = timezone.now()
        long_booking = tbl_booking.objects.create(
            poster_id=self.homeowner,
            booking_type='long_term', rate_period='month',
            booking_status='Pending',
            service_category=['Cleaning'],
            start_time=now + datetime.timedelta(days=1),
            region='Region X - Northern Mindanao',
            province='Misamis Oriental',
            city='Cagayan de Oro',
            barangay='Macasandig',
            street='Macasandig',
            rate_amount=6500.00
        )
        self.client.force_authenticate(user=self.kasambahay)
        prop_url = reverse('booking-proposals-create', kwargs={'booking_id': long_booking.booking_id})
        res = self.client.post(prop_url, {'proposed_rate': '6500.00', 'rate_period': 'month', 'message': 'Standard rate'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)


class BookingMinimumWageEndpointTests(APITestCase):
    def setUp(self):
        self.url = reverse('booking-minimum-wage')

    def test_get_long_term_wage_info_cdo(self):
        res = self.client.get(f"{self.url}?booking_type=long_term&address=Cagayan+de+Oro")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['booking_type'], 'long_term')
        self.assertIsNone(res.data['min_daily_rate'])
        self.assertEqual(res.data['min_monthly_rate'], '6500.00')
        self.assertEqual(res.data['rate_period'], 'month')
        self.assertTrue(res.data['is_statutory_mandatory'])
        self.assertEqual(res.data['wage_order'], 'RBX-DW-06')

    def test_get_long_term_wage_info_ncr(self):
        res = self.client.get(f"{self.url}?booking_type=long_term&address=Quezon+City,+Metro+Manila")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['booking_type'], 'long_term')
        self.assertIsNone(res.data['min_daily_rate'])
        self.assertEqual(res.data['min_monthly_rate'], '7800.00')
        self.assertTrue(res.data['is_statutory_mandatory'])
        self.assertIn('NCR', res.data['wage_order'])

    def test_get_short_term_platform_minimum(self):
        res = self.client.get(f"{self.url}?booking_type=short_term")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['booking_type'], 'short_term')
        self.assertEqual(res.data['min_daily_rate'], '600.00')
        self.assertFalse(res.data['is_statutory_mandatory'])
        self.assertIn('platform minimum', res.data['description'])



