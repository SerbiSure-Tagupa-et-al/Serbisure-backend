from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from datetime import datetime, timezone as datetime_timezone
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase
from notifications.models import tbl_notification
from .availability import available_public_posts, schedule_expired
from .models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal
from .reposting import repost_public_post
from .serializers import RepostScheduleSerializer
from .services import PH_TIME, accept_engagement
from .test_urgent_guards import BookingTransactionTestCase, participants, post

User = get_user_model()


def schedule(days=3, long_term=False):
    start = (timezone.now().astimezone(PH_TIME) + timedelta(days=days)).replace(hour=8, minute=0, second=0, microsecond=0)
    return dict(start_time=start.isoformat(), end_time=(start + timedelta(days=31 if long_term else 0, hours=9)).isoformat(),
        daily_start_time='08:00:00', daily_end_time='17:00:00', work_days=[['M', 'T', 'W', 'Th', 'F', 'S', 'Su'][start.weekday()]])


class PostLifecycleTests(APITestCase):
    def setUp(self):
        self.owner, self.worker = participants()
        self.listing = post(self.owner)
        self.client.force_authenticate(self.owner)

    def repost(self, listing, body=None, key=None):
        return self.client.post(reverse('booking-repost', kwargs={'booking_id': listing.pk}), body or schedule(),
            format='json', HTTP_IDEMPOTENCY_KEY=key or str(uuid4()))

    def test_owner_history_has_all_public_states_and_excludes_private_and_other_users_posts(self):
        expired = post(self.owner, start_time=timezone.now()-timedelta(days=1))
        expected = {str(self.listing.pk), str(expired.pk)}
        for state in ('Accepted', 'InProgress', 'Completed', 'Cancelled'):
            expected.add(str(post(self.owner, booking_status=state).pk))
        post(self.owner, recipient_id=self.worker)
        post(self.worker)
        response = self.client.get(reverse('booking-mine') + '?public_only=true')
        self.assertEqual(response.status_code, 200)
        self.assertEqual({row['booking_id'] for row in response.data}, expected)
        old = next(row for row in response.data if row['booking_id'] == str(expired.pk))
        self.assertEqual(old['post_status'], 'Available')
        self.assertTrue(old['schedule_expired'])
        self.assertTrue(old['can_repost'])
        self.client.force_authenticate(self.worker)
        self.assertNotIn(str(expired.pk), [row['booking_id'] for row in self.client.get(reverse('booking-mine')+'?public_only=true').data])

    def test_public_queries_keep_passed_schedules_and_use_creation_order_for_both_roles(self):
        for poster, viewer in ((self.owner, self.worker), (self.worker, self.owner)):
            future = self.listing if poster == self.owner else post(poster)
            past = [post(poster, booking_type=kind, rate_period='month' if kind == 'long_term' else 'day',
                start_time=timezone.now()-timedelta(days=2)) for kind in ('short_term', 'long_term')]
            expected = [str(row.pk) for row in reversed([future, *past])]
            for state in ('Accepted', 'InProgress', 'Completed', 'Cancelled'):
                post(poster, booking_status=state)
            post(poster, recipient_id=viewer)
            self.client.force_authenticate(viewer)
            for sort in ('newest', 'oldest', 'rate_asc', 'rate_desc'):
                feed = self.client.get(reverse('booking-feed')+'?sort='+sort).data
                self.assertEqual([row['booking_id'] for row in feed], expected)
                self.assertEqual([row['schedule_expired'] for row in feed], [True, True, False])
            profile = self.client.get(reverse('booking-profile-pending', kwargs={'user_id': poster.pk})).data
            self.assertEqual([row['booking_id'] for row in profile], expected)
            self.assertTrue(all(available_public_posts().filter(pk=row.pk).exists() for row in past))

    def test_repost_is_new_idempotent_and_leaves_old_applications_and_content_unchanged(self):
        for poster, applicant in ((self.owner, self.worker), (self.worker, self.owner)):
            source = post(poster, start_time=timezone.now()-timedelta(days=1), special_instruction='Original post')
            old_application = tbl_booking_proposal.objects.create(booking_id=source, proposer_id=applicant, proposed_rate=800)
            self.client.force_authenticate(poster)
            key = str(uuid4())
            first = self.repost(source, key=key)
            self.assertEqual(first.status_code, 201, first.data)
            second = self.repost(source, key=key)
            self.assertEqual(second.status_code, 200, second.data)
            self.assertEqual(second.data['booking_id'], first.data['booking_id'])
            self.assertNotEqual(first.data['booking_id'], str(source.pk))
            fresh = tbl_booking.objects.get(pk=first.data['booking_id'])
            self.assertEqual((fresh.poster_id_id, fresh.special_instruction, fresh.rate_amount), (poster.pk, 'Original post', source.rate_amount))
            self.assertIsNone(fresh.recipient_id_id)
            self.assertFalse(fresh.proposals.exists())
            old_application.refresh_from_db()
            self.assertEqual(old_application.booking_id_id, source.pk)
            self.assertEqual(old_application.status, 'Pending')
            changed = schedule(days=4)
            self.assertEqual(self.repost(source, changed, key).status_code, 400)

    def test_repost_checks_ownership_state_future_schedule_and_long_term_duration(self):
        self.assertEqual(self.repost(self.listing).status_code, 400)
        expired = post(self.owner, start_time=timezone.now()-timedelta(days=1))
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.repost(expired).status_code, 404)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.repost(expired, schedule(days=-1)).status_code, 400)
        booked = post(self.owner, booking_status='Accepted', start_time=timezone.now()-timedelta(days=1))
        self.assertEqual(self.repost(booked).status_code, 400)
        long = post(self.owner, booking_type='long_term', rate_period='month', rate_amount=8000, start_time=timezone.now()-timedelta(days=1))
        self.assertEqual(self.repost(long, schedule()).status_code, 400)
        self.assertEqual(self.repost(long, schedule(long_term=True)).status_code, 201)
        self.assertEqual(self.repost(long, {**schedule(), 'end_time': None}).status_code, 201)

    def test_availability_rejects_inactive_and_assigned_posts(self):
        self.owner.is_active = False
        self.owner.save(update_fields=['is_active'])
        self.assertFalse(available_public_posts().filter(pk=self.listing.pk).exists())
        self.owner.is_active = True
        self.owner.save(update_fields=['is_active'])
        tbl_booking_assignment.objects.create(booking_id=self.listing, accepter_id=self.worker)
        self.assertFalse(available_public_posts().filter(pk=self.listing.pk).exists())

    def test_schedule_badge_changes_at_philippine_midnight(self):
        self.listing.start_time = datetime(2026, 10, 29, 0, tzinfo=datetime_timezone.utc)
        with patch('booking.availability.timezone.now', return_value=datetime(2026, 10, 29, 15, 59, tzinfo=datetime_timezone.utc)):
            self.assertFalse(schedule_expired(self.listing))
        with patch('booking.availability.timezone.now', return_value=datetime(2026, 10, 29, 16, 0, tzinfo=datetime_timezone.utc)):
            self.assertTrue(schedule_expired(self.listing))

    def test_pending_legacy_posts_remain_visible_but_rate_review_still_blocks_acceptance(self):
        self.listing.rate_needs_review = True
        self.listing.save(update_fields=['rate_needs_review'])
        self.assertTrue(available_public_posts().filter(pk=self.listing.pk).exists())
        with self.assertRaises(ValidationError):
            accept_engagement(self.listing.pk, self.worker)

    def test_repost_requires_uuid_and_private_offers_cannot_be_reposted(self):
        expired = post(self.owner, start_time=timezone.now()-timedelta(days=1))
        response = self.client.post(reverse('booking-repost', kwargs={'booking_id': expired.pk}), schedule(), format='json')
        self.assertEqual(response.status_code, 400)
        private = post(self.owner, recipient_id=self.worker, start_time=expired.start_time)
        self.assertEqual(self.repost(private).status_code, 404)

    def test_acceptance_keeps_other_applicants_visible_as_not_selected_and_notifies_them(self):
        other = User.objects.create_user(email='other-worker@post.test', username='other-worker', account_type='Kasambahay', verification_status='Verified')
        winner = tbl_booking_proposal.objects.create(booking_id=self.listing, proposer_id=self.worker, proposed_rate=800)
        loser = tbl_booking_proposal.objects.create(booking_id=self.listing, proposer_id=other, proposed_rate=800)
        accept_engagement(self.listing.pk, self.owner, winner.pk)
        loser.refresh_from_db()
        self.assertEqual(loser.status, 'Rejected')
        self.client.force_authenticate(other)
        applications = self.client.get(reverse('booking-my-proposals')).data
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0]['application_status'], 'not_selected')
        self.assertTrue(tbl_notification.objects.filter(receiver_id=other, notification_message__icontains='not selected').exists())
        response = self.client.post(reverse('booking-proposals-create', kwargs={'booking_id': self.listing.pk}),
            {'apply_at_posted_rate': True}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('already been booked or cancelled', str(response.data))

    def test_fresh_private_booking_leaves_original_post_and_applications_independent(self):
        original = post(self.owner, start_time=timezone.now()-timedelta(days=2))
        application = tbl_booking_proposal.objects.create(booking_id=original, proposer_id=self.worker, proposed_rate=800)
        body = {**schedule(), 'recipient_id': str(self.worker.pk), 'booking_type': 'short_term', 'work_setup': 'stay_out',
            'rate_amount': '800', 'rate_period': 'day', 'region': 'Region X', 'street': 'Agreed address', 'service_category': ['Cleaning']}
        sent = self.client.post(reverse('booking-chat-offer'), body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(sent.status_code, 201, sent.data)
        offer = tbl_booking.objects.get(pk=sent.data['booking']['booking_id'])
        accept_engagement(offer.pk, self.worker)
        original.refresh_from_db(); application.refresh_from_db()
        self.assertEqual((original.booking_status, application.status), ('Pending', 'Pending'))
        self.assertTrue(available_public_posts().filter(pk=original.pk).exists())
        self.assertEqual(tbl_booking_assignment.objects.count(), 1)
        response = self.client.patch(reverse('booking-cancel', kwargs={'booking_id': original.pk}),
            {'reason': 'Other', 'expected_points': 0, 'pending_only': True}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(available_public_posts().filter(pk=original.pk).exists())
        offer.refresh_from_db()
        self.assertEqual(offer.booking_status, 'Accepted')

    def test_cancel_post_rechecks_ownership_and_cannot_cancel_a_newly_accepted_contract(self):
        url = reverse('booking-cancel', kwargs={'booking_id': self.listing.pk})
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.client.patch(url, {'reason': 'Other', 'pending_only': True}, format='json').status_code, 403)
        accept_engagement(self.listing.pk, self.worker)
        self.client.force_authenticate(self.owner)
        response = self.client.patch(url, {'reason': 'Other', 'expected_points': 0, 'pending_only': True}, format='json')
        self.assertEqual(response.status_code, 400)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.booking_status, 'Accepted')

    def test_removal_migration_leaves_no_source_column(self):
        from django.db import connection
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, 'tbl_booking')
        self.assertNotIn('source_post_id', {column.name for column in columns})


class ConcurrentPostLifecycleTests(BookingTransactionTestCase):
    def test_concurrent_repost_retries_create_only_one_new_listing(self):
        owner, worker = participants()
        source = post(owner, start_time=timezone.now()-timedelta(days=1))
        serializer = RepostScheduleSerializer(data=schedule())
        serializer.is_valid(raise_exception=True)
        key, barrier = uuid4(), Barrier(2)
        def repost(_):
            close_old_connections()
            try:
                actor = User.objects.get(pk=owner.pk)
                barrier.wait(timeout=10)
                row, created = repost_public_post(source.pk, actor, serializer.validated_data, key)
                return row.pk, created
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(repost, range(2)))
        self.assertEqual(len({row_id for row_id, _ in outcomes}), 1)
        self.assertCountEqual([created for _, created in outcomes], [True, False])
        self.assertEqual(tbl_booking.objects.count(), 2)

    def test_two_applicants_cannot_both_be_accepted(self):
        owner, worker = participants()
        other = User.objects.create_user(email='concurrent-worker@post.test', username='concurrent-worker', account_type='Kasambahay', verification_status='Verified')
        listing = post(owner)
        proposals = [tbl_booking_proposal.objects.create(booking_id=listing, proposer_id=person, proposed_rate=800) for person in (worker, other)]
        barrier = Barrier(2)
        def accept(proposal):
            close_old_connections()
            try:
                actor = User.objects.get(pk=owner.pk)
                barrier.wait(timeout=10)
                accept_engagement(listing.pk, actor, proposal.pk)
                return 'accepted'
            except ValidationError:
                return 'closed'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(accept, proposals)), ['accepted', 'closed'])
        self.assertEqual(tbl_booking_assignment.objects.count(), 1)
