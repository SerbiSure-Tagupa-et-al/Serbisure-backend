from datetime import timedelta
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from rest_framework.exceptions import ValidationError
from .models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal
from .services import accept_engagement, month_bounds, pair_booking_count
from .wage_policy import validate_rate
from verifications.models import tbl_documents

User = get_user_model()


def participants():
    return (User.objects.create_user(email='guard-owner@test.com', username='guard-owner',
                                     password='StrongPassword123!', account_type='Homeowner', verification_status='Verified'),
            User.objects.create_user(email='guard-worker@test.com', username='guard-worker',
                                     password='StrongPassword123!', account_type='Kasambahay', verification_status='Verified'))


def post(poster, **overrides):
    values = dict(poster_id=poster, booking_type='short_term', rate_period='day',
                  rate_amount=Decimal('800'), service_category=['Cleaning'], region='Region X',
                  city='Cagayan de Oro', start_time=timezone.now()+timedelta(days=2))
    values.update(overrides)
    return tbl_booking.objects.create(**values)


class EngagementGuardTests(APITestCase):
    def setUp(self):
        self.owner, self.worker = participants()
        self.client.force_authenticate(self.worker)

    def accept(self, booking):
        return self.client.patch(reverse('booking-accept', kwargs={'booking_id': booking.pk}), {}, format='json')

    def proposal(self, booking, amount='800', period='day'):
        return self.client.post(reverse('booking-proposals-create', kwargs={'booking_id': booking.pk}),
                                {'proposed_rate': amount, 'rate_period': period}, format='json')

    def test_monthly_amount_is_stored_and_returned_without_conversion(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(reverse('booking-post'), {
            'booking_type': 'long_term', 'work_setup': 'stay_out', 'rate_amount': '6500.00', 'rate_period': 'month',
            'region': 'Region X', 'city': 'Cagayan de Oro', 'service_category': ['Cleaning'],
            'start_time': (timezone.now()+timedelta(days=2)).isoformat(),
        }, format='json', HTTP_IDEMPOTENCY_KEY='11bc428f-37ea-4b64-83ac-841695151d27')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['data']['rate_amount'], '6500.00')
        self.assertEqual(response.data['data']['rate_period'], 'month')
        self.assertNotIn('daily_rate', response.data['data'])

    def test_same_floor_applies_to_post_proposal_and_acceptance(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(reverse('booking-post'), {
            'booking_type': 'short_term', 'work_setup': 'stay_out', 'rate_amount': '599', 'rate_period': 'day',
            'service_category': ['Cleaning'], 'start_time': (timezone.now()+timedelta(days=2)).isoformat(),
        }, format='json', HTTP_IDEMPOTENCY_KEY='3d71f08c-c138-4a8c-8e30-1dba9972b3d5')
        self.assertEqual(response.status_code, 400)
        self.client.force_authenticate(self.worker)
        listing = post(self.owner, rate_amount='599')
        self.assertEqual(self.accept(listing).status_code, 400)
        self.assertEqual(self.proposal(listing, '599').status_code, 400)
        listing.refresh_from_db()
        self.assertEqual(listing.booking_status, 'Pending')
        self.assertFalse(listing.assignments.exists())

    def test_monthly_floor_and_period_are_checked(self):
        listing = post(self.owner, booking_type='long_term', rate_period='month', rate_amount='6499')
        self.assertEqual(self.accept(listing).status_code, 400)
        self.assertEqual(self.proposal(listing, '6499', 'month').status_code, 400)
        self.assertEqual(self.proposal(listing, '6500', 'day').status_code, 400)
        with self.assertRaises(ValidationError):
            validate_rate('6500', 'long_term', 'NCR', 'Manila 9000')
        self.assertEqual(validate_rate('7800', 'long_term', 'NCR', 'Manila 9000'), Decimal('7800'))
        with self.assertRaises(ValidationError):
            validate_rate('6500', 'long_term', 'Region VII')
        for bad in ('NaN', 'Infinity', '-1', '800.001'):
            with self.assertRaises(ValidationError):
                validate_rate(bad, 'short_term')

    def test_fourth_short_booking_blocked_and_long_term_allowed(self):
        for _ in range(3):
            self.assertEqual(self.accept(post(self.owner)).status_code, 200)
        fourth = post(self.owner)
        response = self.accept(fourth)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(str(response.data['code']), 'short_term_monthly_cap')
        self.assertEqual(self.proposal(fourth).status_code, 400)
        long = post(self.owner, booking_type='long_term', rate_period='month', rate_amount='6500')
        response = self.accept(long)
        self.assertEqual(response.status_code, 200)
        summary = response.data['booking']['contract_summary']
        self.assertEqual(summary['rate_amount'], '6500.00')
        self.assertIn('contract-template', summary['contract_pdf_url'])

    def test_cap_is_pair_specific_reverse_direction_and_month_specific(self):
        listing = post(self.worker)
        accept_engagement(listing.pk, self.owner)
        self.assertEqual(pair_booking_count(self.owner.pk, self.worker.pk, listing.start_time), 1)
        start, _ = month_bounds(listing.start_time)
        tbl_booking.objects.filter(pk=listing.pk).update(start_time=start-timedelta(seconds=1))
        self.assertEqual(pair_booking_count(self.owner.pk, self.worker.pk, start), 0)
        accept_engagement(post(self.owner).pk, self.worker)
        self.assertEqual(pair_booking_count(self.worker.pk, self.owner.pk, start), 1)

    def test_advance_booking_cannot_evade_service_month_cap(self):
        _, next_month = month_bounds()
        date = next_month + timedelta(days=2)
        for _ in range(3):
            listing = post(self.owner, start_time=date)
            accept_engagement(listing.pk, self.worker)
            tbl_booking_assignment.objects.filter(booking_id=listing).update(accepted_at=timezone.now()-timedelta(days=60))
        self.assertEqual(self.accept(post(self.owner, start_time=date)).status_code, 400)

    def test_accepted_cancellation_does_not_evade_cap(self):
        for _ in range(3):
            listing = post(self.owner)
            accept_engagement(listing.pk, self.worker)
            listing.booking_status = 'Cancelled'
            listing.save(update_fields=['booking_status'])
        self.assertEqual(self.accept(post(self.owner)).status_code, 400)

    def test_both_parties_must_be_verified_current_and_correct_roles(self):
        listing = post(self.owner)
        tbl_documents.objects.filter(user_profile=self.owner).update(verification_status='Rejected')
        self.assertEqual(self.accept(listing).status_code, 403)
        tbl_documents.objects.filter(user_profile=self.owner).update(verification_status='Verified', valid_until=timezone.localdate()-timedelta(days=1))
        self.assertEqual(self.accept(listing).status_code, 403)
        tbl_documents.objects.filter(user_profile=self.owner).update(valid_until=None)
        self.worker.is_restricted = True
        self.worker.save(update_fields=['is_restricted'])
        self.assertEqual(self.accept(listing).status_code, 403)
        self.worker.is_restricted = False
        self.worker.save(update_fields=['is_restricted'])
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.accept(listing).status_code, 400)
        other = User.objects.create_user(email='other-owner@test.com', username='other-owner', account_type='Homeowner', verification_status='Verified')
        self.client.force_authenticate(other)
        self.assertEqual(self.accept(listing).status_code, 403)

    def test_expired_and_legacy_unreviewed_posts_cannot_be_accepted(self):
        self.assertEqual(self.accept(post(self.owner, start_time=timezone.now()-timedelta(days=1))).status_code, 400)
        legacy = post(self.owner, booking_type='long_term', rate_period='month', rate_amount='8000', rate_needs_review=True)
        self.assertEqual(self.accept(legacy).status_code, 400)
        self.client.force_authenticate(self.owner)
        res = self.client.patch(reverse('booking-rate-review', kwargs={'booking_id': legacy.pk}),
                                {'rate_amount': '8000', 'rate_period': 'month'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.accept(legacy).status_code, 200)

    def test_proposal_acceptance_rechecks_cap_verification_and_booking_state(self):
        listing = post(self.owner)
        proposal = tbl_booking_proposal.objects.create(booking_id=listing, proposer_id=self.worker, proposed_rate='800')
        for _ in range(3):
            accept_engagement(post(self.owner).pk, self.worker)
        self.client.force_authenticate(self.owner)
        url = reverse('booking-proposals-respond', kwargs={'proposal_id': proposal.pk})
        self.assertEqual(self.client.patch(url, {'action': 'accept'}, format='json').status_code, 400)
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, 'Pending')
        listing.booking_status = 'Cancelled'
        listing.save(update_fields=['booking_status'])
        self.assertEqual(self.client.patch(url, {'action': 'accept'}, format='json').status_code, 400)
        listing.refresh_from_db()
        self.assertEqual(listing.booking_status, 'Cancelled')

    def test_direct_acceptance_invalidates_proposals_and_cannot_replace_assignment(self):
        listing = post(self.owner)
        proposal = tbl_booking_proposal.objects.create(booking_id=listing, proposer_id=self.worker, proposed_rate='800')
        self.assertEqual(self.accept(listing).status_code, 200)
        self.client.force_authenticate(self.owner)
        response = self.client.patch(reverse('booking-proposals-respond', kwargs={'proposal_id': proposal.pk}), {'action': 'accept'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(listing.assignments.count(), 1)

    def test_start_and_completion_require_real_start(self):
        listing = post(self.owner)
        accept_engagement(listing.pk, self.worker)
        start_url = reverse('booking-start', kwargs={'booking_id': listing.pk})
        complete_url = reverse('booking-complete', kwargs={'booking_id': listing.pk})
        self.assertEqual(self.client.patch(complete_url).status_code, 400)
        self.assertEqual(self.client.patch(start_url).status_code, 400)
        listing.start_time = timezone.now()-timedelta(minutes=2)
        listing.save(update_fields=['start_time'])
        self.assertEqual(self.client.patch(start_url).status_code, 200)
        self.assertEqual(self.client.patch(complete_url).status_code, 200)
        listing.refresh_from_db()
        self.assertIsNotNone(listing.started_at)
        self.assertIsNotNone(listing.completed_at)
        self.assertEqual(self.client.patch(complete_url).status_code, 400)

    def test_official_blank_pdf_is_downloadable_without_exposing_terms(self):
        self.client.force_authenticate(None)
        response = self.client.get(reverse('booking-contract-template'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF'))
        self.assertIn('attachment', response['Content-Disposition'])


class BookingTransactionTestCase(TransactionTestCase):
    def _fixture_teardown(self):
        # As in realtime tests, permit fixture truncation only in the disposable local test DB.
        if connection.vendor != 'postgresql':
            return super()._fixture_teardown()
        if (not connection.settings_dict['NAME'].startswith('test_')
                or connection.settings_dict['HOST'] not in ('localhost', '127.0.0.1', '::1')):
            raise RuntimeError('Audit fixture cleanup requires an isolated local test database.')
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE tbl_account_access_audit DISABLE TRIGGER account_access_audit_immutable')
            cursor.execute('ALTER TABLE tbl_penalty_adjustment DISABLE TRIGGER penalty_adjustment_immutable')
        try:
            return super()._fixture_teardown()
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE tbl_account_access_audit ENABLE TRIGGER account_access_audit_immutable')
                cursor.execute('ALTER TABLE tbl_penalty_adjustment ENABLE TRIGGER penalty_adjustment_immutable')


class ConcurrentAcceptanceTests(BookingTransactionTestCase):
    def setUp(self):
        self.owner, self.worker = participants()

    def test_pair_cap_serializes_different_posts(self):
        for _ in range(2):
            accept_engagement(post(self.owner).pk, self.worker)
        listings = [post(self.owner), post(self.owner)]
        barrier = Barrier(2)
        def accept(pk):
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.worker.pk)
                barrier.wait(timeout=10)
                accept_engagement(pk, actor)
                return 'accepted'
            except ValidationError:
                return 'blocked'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(accept, [listing.pk for listing in listings]))
        self.assertCountEqual(results, ['accepted', 'blocked'])
        self.assertEqual(pair_booking_count(self.owner.pk, self.worker.pk, listings[0].start_time), 3)




class PenaltyPointsMigrationTests(BookingTransactionTestCase):
    def test_restriction_migration_preserves_existing_columns_and_values(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from importlib import import_module
        owner, _ = participants()
        owner.penalty_points = 2
        owner.is_restricted = True
        owner.save(update_fields=['penalty_points', 'is_restricted'])
        executor = MigrationExecutor(connection)
        previous = [('accounts', '0020_tbl_user_profile_chat_suspended_until_and_more')]
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(previous)
            old_apps = executor.loader.project_state(previous).apps
            old_user = old_apps.get_model('accounts', 'tbl_user_profile').objects.get(pk=owner.pk)
            self.assertEqual(old_user.cancellation_strikes, 2)
            migration = import_module('accounts.migrations.0018_engagement_restrictions')
            with connection.schema_editor() as editor:
                migration.ensure_restriction_columns(old_apps, editor)
        finally:
            MigrationExecutor(connection).migrate(latest)
        owner.refresh_from_db()
        self.assertEqual(owner.penalty_points, 2)
        self.assertTrue(owner.is_restricted)


class CompensationMigrationTests(BookingTransactionTestCase):
    def test_legacy_amounts_are_preserved_and_require_explicit_monthly_review(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        owner, _ = participants()
        row_id = post(owner, booking_type='long_term', rate_period='month', rate_amount='8000').pk
        previous = [('booking', '0011_tbl_saved_post')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(previous)
            old_apps = executor.loader.project_state(previous).apps
            OldBooking = old_apps.get_model('booking', 'tbl_booking')
            self.assertEqual(OldBooking.objects.get(pk=row_id).daily_rate, Decimal('8000.00'))
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
            row = tbl_booking.objects.get(pk=row_id)
            self.assertEqual(row.rate_amount, Decimal('8000.00'))
            self.assertEqual(row.rate_period, 'month')
            self.assertTrue(row.rate_needs_review)
        finally:
            MigrationExecutor(connection).migrate(latest)
