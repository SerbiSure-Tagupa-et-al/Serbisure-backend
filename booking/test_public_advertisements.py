from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import tbl_booking, tbl_booking_assignment
from .services import PH_TIME
from . import test_chat_offers
from .test_urgent_guards import participants, post


class PublicAdvertisementTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.owner, self.worker = participants()

    def body(self, long_term=False):
        return dict(
            booking_type='long_term' if long_term else 'short_term', work_setup='stay_out',
            service_category=['Cleaning'], daily_start_time='08:00:00', daily_end_time='17:00:00',
            region='Region X', province='Misamis Oriental', city='Cagayan de Oro', barangay='Carmen',
            street='12 Example Street', zip_code='9000', special_instruction='Please bring gloves.',
            rate_amount='8000' if long_term else '800', rate_period='month' if long_term else 'day',
        )

    def send(self, sender, body, key=None):
        self.client.force_authenticate(sender)
        return self.client.post(reverse('booking-post'), body, format='json',
                                HTTP_IDEMPOTENCY_KEY=key or str(uuid4()))

    def test_both_roles_post_without_calendar_terms_and_preserve_daily_hours(self):
        now = datetime(2026, 11, 2, 23, 30, tzinfo=PH_TIME)
        with patch('booking.serializers.timezone.now', return_value=now):
            for sender in (self.owner, self.worker):
                for long_term in (False, True):
                    body = self.body(long_term)
                    response = self.send(sender, body)
                    self.assertEqual(response.status_code, 201, response.data)
                    row = tbl_booking.objects.get(pk=response.data['data']['booking_id'])
                    self.assertEqual(row.start_time.astimezone(PH_TIME), now)
                    self.assertIsNone(row.end_time)
                    self.assertEqual(row.work_days, [])
                    self.assertIsNone(row.recipient_id_id)
                    self.assertEqual(row.booking_status, 'Pending')
                    for field in ('daily_start_time', 'daily_end_time', 'street', 'zip_code', 'rate_period'):
                        self.assertEqual(str(getattr(row, field)), body[field])

    def test_legacy_calendar_input_is_ignored_and_idempotency_still_works(self):
        body = {**self.body(), 'start_time': '2000-01-05T08:00:00+08:00',
                'end_time': '2000-01-01T17:00:00+08:00', 'work_days': ['M', 'M']}
        key = str(uuid4())
        first = self.send(self.owner, body, key)
        self.assertEqual(first.status_code, 201, first.data)
        second = self.send(self.owner, body, key)
        self.assertEqual(second.data, first.data)
        self.assertEqual(tbl_booking.objects.count(), 1)
        self.assertEqual(first.data['data']['work_days'], [])
        self.assertIsNone(first.data['data']['end_time'])

    def test_storage_timestamp_is_not_a_future_contract_even_before_the_shift(self):
        from rest_framework.exceptions import ValidationError
        from .services import ensure_open

        now = datetime(2026, 11, 2, 7, 0, tzinfo=PH_TIME)
        with patch('booking.serializers.timezone.now', return_value=now):
            response = self.send(self.owner, self.body())
            self.assertEqual(response.status_code, 201, response.data)
            row = tbl_booking.objects.get(pk=response.data['data']['booking_id'])
            with self.assertRaises(ValidationError) as error:
                ensure_open(row)
            self.assertIn('Please send a new offer in chat', str(error.exception))

    def test_daily_hours_and_minimum_rate_are_still_validated(self):
        for updates in ({'daily_start_time': '17:00:00', 'daily_end_time': '08:00:00'},
                        {'daily_end_time': None}, {'daily_start_time': 'bad'},
                        {'rate_amount': '599'}, {'rate_period': 'month'}):
            with self.subTest(updates=updates):
                response = self.send(self.owner, {**self.body(), **updates})
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(tbl_booking.objects.exists())

    def test_pending_public_posts_remain_in_feed_regardless_of_old_storage_date(self):
        for poster, viewer in ((self.owner, self.worker), (self.worker, self.owner)):
            listing = post(poster, start_time=timezone.now() - timedelta(days=365))
            cancelled = post(poster, booking_status='Cancelled')
            assigned = post(poster)
            tbl_booking_assignment.objects.create(booking_id=assigned, accepter_id=viewer)
            self.client.force_authenticate(viewer)
            ids = [row['booking_id'] for row in self.client.get(reverse('booking-feed')).data]
            self.assertIn(str(listing.pk), ids)
            self.assertNotIn(str(cancelled.pk), ids)
            self.assertNotIn(str(assigned.pk), ids)


class ChatCalendarIsolationTests(APITestCase):
    """Explicit regression cases for the unchanged chat serializer."""

    setUp = test_chat_offers.ChatOfferTests.setUp
    payload = test_chat_offers.ChatOfferTests.payload

    def test_chat_offers_still_require_dates_weekdays_and_hours(self):
        self.client.force_authenticate(self.owner)
        original = {**self.payload(self.worker), 'booking_type': 'short_term', 'rate_amount': '800', 'rate_period': 'day'}
        for field in ('start_time', 'end_time', 'work_days', 'daily_start_time', 'daily_end_time'):
            body = {key: value for key, value in original.items() if key != field}
            with self.subTest(field=field):
                response = self.client.post(reverse('booking-chat-offer'), body, format='json',
                                            HTTP_IDEMPOTENCY_KEY=str(uuid4()))
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(tbl_booking.objects.exists())
