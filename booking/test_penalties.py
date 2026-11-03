from accounts.testing import create_verified_test_user
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APITestCase
from accounts.penalties import assert_booking_unrestricted, refresh_monthly_penalties
from rest_framework.exceptions import PermissionDenied
from booking.cancellation import SAFETY_REASON
from booking.services import PH_TIME
from booking.models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal

User = get_user_model()


class BookingPenaltyTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.owner = create_verified_test_user(username='penalty-owner', email='penalty-owner@test.com', password='test', account_type='Homeowner', verification_status='Verified')
        self.worker = create_verified_test_user(username='penalty-worker', email='penalty-worker@test.com', password='test', account_type='Kasambahay', verification_status='Verified')
        self.now = datetime(2026, 10, 20, 8, tzinfo=PH_TIME)

    def booking(self, **kwargs):
        defaults = dict(poster_id=self.owner, booking_type='short_term', booking_status='Accepted', rate_period='day',
                        rate_amount='800', service_category=['Cleaning'], region='Region X', start_time=self.now + timedelta(hours=12))
        defaults.update(kwargs)
        booking = tbl_booking.objects.create(**defaults)
        if booking.booking_status != 'Pending':
            tbl_booking_assignment.objects.create(booking_id=booking, accepter_id=self.worker)
        return booking

    def cancel(self, booking, actor=None, **kwargs):
        self.client.force_authenticate(actor or self.owner)
        reason = 'Other'
        with patch('django.utils.timezone.now', return_value=self.now):
            return self.client.patch(reverse('booking-cancel', kwargs={'booking_id': booking.pk}), {'reason': reason, **kwargs}, format='json')

    def test_timing_boundaries_for_both_roles_and_both_booking_types(self):
        for kind in ('short_term', 'long_term'):
            for actor in (self.owner, self.worker):
                for delta, expected in ((timedelta(hours=25), 0), (timedelta(hours=24), 0), (timedelta(hours=23), 1), (timedelta(0), 2), (timedelta(hours=-2), 2)):
                    with self.subTest(kind=kind, role=actor.account_type, delta=delta):
                        User.objects.filter(pk=actor.pk).update(penalty_points=0, is_restricted=False, penalty_month=self.now.date().replace(day=1))
                        booking = self.booking(booking_type=kind, rate_period='month' if kind == 'long_term' else 'day', start_time=self.now + delta)
                        response = self.cancel(booking, actor)
                        self.assertEqual(response.status_code, 200, response.data)
                        self.assertEqual(response.data['penalty']['points_added'], expected)
                        actor.refresh_from_db()
                        self.assertEqual(actor.penalty_points, expected)
                        booking.refresh_from_db()
                        self.assertEqual(booking.cancellation_reason, 'Other')
                        self.assertEqual(booking.cancelled_by_id, actor.pk)

    def test_threshold_and_retries_do_not_charge_the_other_person_or_twice(self):
        self.owner.penalty_points = 2
        self.owner.save(update_fields=['penalty_points'])
        booking = self.booking(start_time=self.now)
        first = self.cancel(booking)
        second = self.cancel(booking)
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data['penalty']['points_added'], 0)
        self.owner.refresh_from_db(); self.worker.refresh_from_db()
        self.assertEqual(self.owner.penalty_points, 3)
        self.assertTrue(self.owner.is_restricted)
        self.assertEqual(self.worker.penalty_points, 0)

    def test_pending_post_or_offer_is_free_but_requires_a_reason(self):
        booking = self.booking(booking_status='Pending', recipient_id=self.worker, start_time=self.now - timedelta(hours=1))
        self.client.force_authenticate(self.worker)
        response = self.client.patch(reverse('booking-cancel', kwargs={'booking_id': booking.pk}), {}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.cancel(booking, self.worker).data['penalty']['points_added'], 0)
        retry = self.cancel(booking, self.worker)
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(retry.data['penalty']['points_added'], 0)

    def test_reason_validation_authorization_and_completed_bookings(self):
        booking = self.booking()
        self.assertEqual(self.cancel(booking, reason=SAFETY_REASON).status_code, 400)
        self.assertEqual(self.cancel(booking, reason='').status_code, 400)
        outsider = User.objects.create_user(username='outsider-penalty', email='outsider-penalty@test.com', password='test', account_type='Homeowner')
        self.assertEqual(self.cancel(booking, outsider).status_code, 403)
        self.assertEqual(self.cancel(self.booking(booking_status='Completed')).status_code, 400)
        booking.refresh_from_db()
        self.assertEqual(booking.booking_status, 'Accepted')

    def test_safety_reason_does_not_skip_points_and_requests_advisory(self):
        for offset, points in ((timedelta(hours=10), 1), (timedelta(hours=-1), 2), (timedelta(hours=25), 0)):
            response = self.cancel(self.booking(start_time=self.now + offset), self.worker, reason=SAFETY_REASON)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data['penalty']['points_added'], points)
            self.assertTrue(response.data['safety_advisory'])

    def test_preview_and_timing_change_are_authoritative(self):
        booking = self.booking()
        self.client.force_authenticate(self.worker)
        with patch('django.utils.timezone.now', return_value=self.now):
            preview = self.client.get(reverse('booking-cancel', kwargs={'booking_id': booking.pk}))
        self.assertEqual(preview.data['points_to_add'], 1)
        self.assertIn(SAFETY_REASON, preview.data['reasons'])
        response = self.cancel(booking, self.worker, expected_points=0)
        self.assertEqual(response.status_code, 400)
        booking.refresh_from_db()
        self.assertEqual(booking.booking_status, 'Accepted')

    def test_first_request_after_philippine_midnight_resets_the_month(self):
        User.objects.filter(pk=self.worker.pk).update(penalty_points=3, is_restricted=True, is_active=False,
            is_account_suspended=True, penalty_month=datetime(2026, 12, 1).date())
        before_midnight = datetime(2026, 12, 31, 23, 59, tzinfo=PH_TIME)
        self.worker.refresh_from_db()
        with patch('django.utils.timezone.now', return_value=before_midnight):
            refresh_monthly_penalties(self.worker)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.penalty_points, 3)
        with patch('django.utils.timezone.now', return_value=before_midnight + timedelta(minutes=1)):
            refresh_monthly_penalties(self.worker)
            refresh_monthly_penalties(self.worker)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.penalty_points, 0)
        self.assertFalse(self.worker.is_restricted)
        self.assertTrue(self.worker.is_account_suspended)
        self.assertFalse(self.worker.is_active)
        self.assertEqual(self.worker.penalty_month, datetime(2027, 1, 1).date())

    def test_expired_month_clears_points_and_restriction_at_any_balance(self):
        for points in (0, 2, 3):
            with self.subTest(points=points):
                User.objects.filter(pk=self.owner.pk).update(penalty_month=datetime(2026, 9, 1).date(), penalty_points=points, is_restricted=True)
                self.owner.refresh_from_db()
                with patch('django.utils.timezone.now', return_value=self.now):
                    refresh_monthly_penalties(self.owner)
                self.owner.refresh_from_db()
                self.assertEqual(self.owner.penalty_points, 0)
                self.assertFalse(self.owner.is_restricted)
                self.assertEqual(self.owner.penalty_month, self.now.date().replace(day=1))

    def test_current_month_points_are_preserved_and_threshold_blocks_even_without_flag(self):
        for points, restricted in ((2, False), (3, True), (3, False)):
            with self.subTest(points=points, restricted=restricted):
                User.objects.filter(pk=self.owner.pk).update(penalty_month=self.now.date().replace(day=1), penalty_points=points, is_restricted=restricted)
                self.owner.refresh_from_db()
                with patch('django.utils.timezone.now', return_value=self.now):
                    if points >= 3:
                        with self.assertRaises(PermissionDenied):
                            assert_booking_unrestricted(self.owner)
                    else:
                        assert_booking_unrestricted(self.owner)
                self.owner.refresh_from_db()
                self.assertEqual(self.owner.penalty_points, points)

    def test_every_booking_action_resets_an_expired_month_for_both_roles(self):
        start = self.now + timedelta(days=2)
        payload = dict(booking_type='long_term', work_setup='stay_out', service_category=['Cleaning'],
            rate_amount='8000', rate_period='month', region='Region X', city='Cagayan de Oro',
            street='Test agreed work address', start_time=start.isoformat(),
            end_time=(start + timedelta(days=30, hours=9)).isoformat(), daily_start_time='08:00:00',
            daily_end_time='17:00:00', work_days=[['M', 'T', 'W', 'Th', 'F', 'S', 'Su'][start.weekday()]])
        for actor, partner in ((self.owner, self.worker), (self.worker, self.owner)):
            for action in ('post', 'propose', 'accept', 'offer', 'accept_proposal'):
                with self.subTest(role=actor.account_type, action=action):
                    User.objects.filter(pk__in=[actor.pk, partner.pk]).update(penalty_points=0, is_restricted=False,
                        penalty_month=self.now.date().replace(day=1))
                    User.objects.filter(pk=actor.pk).update(penalty_points=3, is_restricted=True,
                        penalty_month=datetime(2026, 9, 1).date())
                    actor.refresh_from_db()
                    self.client.force_authenticate(actor)
                    listing = self.booking(booking_status='Pending', poster_id=actor if action == 'accept_proposal' else partner,
                        booking_type='long_term', rate_period='month', rate_amount='8000', start_time=start)
                    with patch('django.utils.timezone.now', return_value=self.now):
                        if action == 'post':
                            response = self.client.post(reverse('booking-post'), payload, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
                        elif action == 'propose':
                            response = self.client.post(reverse('booking-proposals-create', kwargs={'booking_id': listing.pk}),
                                {'proposed_rate': '8000', 'rate_period': 'month'}, format='json')
                        elif action == 'accept':
                            response = self.client.patch(reverse('booking-accept', kwargs={'booking_id': listing.pk}), {}, format='json')
                        elif action == 'offer':
                            response = self.client.post(reverse('booking-chat-offer'), {**payload, 'recipient_id': str(partner.pk)},
                                format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
                        else:
                            proposal = tbl_booking_proposal.objects.create(booking_id=listing, proposer_id=partner, proposed_rate='8000')
                            response = self.client.patch(reverse('booking-proposals-respond', kwargs={'proposal_id': proposal.pk}),
                                {'action': 'accept'}, format='json')
                    self.assertIn(response.status_code, (200, 201), response.data)
                    actor.refresh_from_db()
                    self.assertEqual(actor.penalty_points, 0)
                    self.assertFalse(actor.is_restricted)
                    self.assertEqual(actor.penalty_month, self.now.date().replace(day=1))

    def test_cancellation_migration_adopts_existing_columns_without_replacing_data(self):
        from django.apps import apps
        from django.db import connection
        from importlib import import_module
        booking = self.booking(cancellation_reason='Existing reason', cancelled_by=self.worker,
                               cancellation_penalty_points=2)
        migration = import_module('booking.migrations.0015_booking_cancellation_details')
        with connection.schema_editor() as editor:
            migration.ensure_cancellation_columns(apps, editor)
        booking.refresh_from_db()
        self.assertEqual(booking.cancellation_reason, 'Existing reason')
        self.assertEqual(booking.cancelled_by_id, self.worker.pk)
        self.assertEqual(booking.cancellation_penalty_points, 2)

    def test_threshold_blocks_posting_proposals_acceptance_and_chat_offers(self):
        User.objects.filter(pk=self.worker.pk).update(penalty_points=3, is_restricted=True)
        self.client.force_authenticate(self.worker)
        booking = self.booking(booking_status='Pending')
        endpoints = [
            ('post', reverse('booking-post'), {}, {'HTTP_IDEMPOTENCY_KEY': str(uuid4())}),
            ('patch', reverse('booking-accept', kwargs={'booking_id': booking.pk}), {}, {}),
            ('post', reverse('booking-proposals-create', kwargs={'booking_id': booking.pk}), {'proposed_rate': '800', 'rate_period': 'day'}, {}),
            ('post', reverse('booking-chat-offer'), {'recipient_id': str(self.owner.pk)}, {'HTTP_IDEMPOTENCY_KEY': str(uuid4())}),
        ]
        for method, path, data, headers in endpoints:
            with self.subTest(path=path):
                self.assertEqual(getattr(self.client, method)(path, data, format='json', **headers).status_code, 403)
        # History remains accessible, and even restricted users can cancel existing work.
        self.assertEqual(self.client.get(reverse('booking-assigned')).status_code, 200)
        self.assertEqual(self.cancel(self.booking(), self.worker).status_code, 200)
