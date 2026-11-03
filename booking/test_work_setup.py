from accounts.testing import create_verified_test_user
from datetime import timedelta
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import tbl_booking
from .services import accept_engagement

User = get_user_model()


class WorkSetupTests(APITestCase):
    def setUp(self):
        self.owner = create_verified_test_user(email='setup-owner@test.com', username='setup-owner',
            password='test', account_type='Homeowner', verification_status='Verified')
        self.worker = create_verified_test_user(email='setup-worker@test.com', username='setup-worker',
            password='test', account_type='Kasambahay', verification_status='Verified', user_tags=['Stay-In'])
        self.client.force_authenticate(self.owner)

    def payload(self, booking_type='long_term', work_setup='stay_out'):
        return dict(booking_type=booking_type, work_setup=work_setup, service_category=['Cleaning'],
            region='Region X', rate_amount='8000' if booking_type == 'long_term' else '800',
            rate_period='month' if booking_type == 'long_term' else 'day',
            start_time=(timezone.now()+timedelta(days=2)).isoformat())

    def create(self, data):
        return self.client.post(reverse('booking-post'), data, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))

    def test_setup_is_required_and_validated(self):
        for value in (None, '', 'invalid', 'Stay-In'):
            response = self.create(self.payload(work_setup=value))
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn('work_setup', response.data)
        data = self.payload()
        data.pop('work_setup')
        self.assertEqual(self.create(data).status_code, 400)
        self.assertEqual(tbl_booking.objects.count(), 0)

    def test_all_engagement_and_setup_combinations_persist_without_changing_profile(self):
        for role in (self.owner, self.worker):
            self.client.force_authenticate(role)
            for kind in ('short_term', 'long_term'):
                for setup in ('stay_in', 'stay_out'):
                    response = self.create(self.payload(kind, setup))
                    self.assertEqual(response.status_code, 201, response.data)
                    row = tbl_booking.objects.get(pk=response.data['data']['booking_id'])
                    self.assertEqual(row.work_setup, setup)
                    self.assertEqual(response.data['data']['work_setup'], setup)
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.user_tags, ['Stay-In'])

    def test_feed_filters_use_setup_field_independently_of_duration_and_description(self):
        for kind in ('short_term', 'long_term'):
            for setup in ('stay_in', 'stay_out'):
                data = self.payload(kind, setup)
                data['special_instruction'] = 'A misleading stay-in setup description.'
                self.assertEqual(self.create(data).status_code, 201)
        self.client.force_authenticate(self.worker)
        response = self.client.get(reverse('booking-feed'), {'work_setup': 'stay_out'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        self.assertEqual({item['booking_type'] for item in response.data}, {'short_term', 'long_term'})
        self.assertEqual({item['work_setup'] for item in response.data}, {'stay_out'})
        response = self.client.get(reverse('booking-feed'), {'work_setup': 'stay_out', 'booking_type': 'long_term'})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(self.client.get(reverse('booking-feed'), {'work_setup': 'invalid'}).status_code, 400)
        self.assertEqual(self.client.get(reverse('booking-feed'), {'booking_type': 'stay_in'}).status_code, 400)

    def test_setup_is_in_saved_public_profile_and_participant_contract_summary(self):
        response = self.create(self.payload())
        row = tbl_booking.objects.get(pk=response.data['data']['booking_id'])
        self.client.force_authenticate(self.worker)
        response = self.client.get(reverse('booking-profile-pending', kwargs={'user_id': self.owner.pk}))
        self.assertEqual(response.data[0]['work_setup'], 'stay_out')
        self.client.post(reverse('booking-save-toggle'), {'booking_id': str(row.pk)}, format='json')
        self.assertEqual(self.client.get(reverse('booking-saved-list')).data[0]['work_setup'], 'stay_out')
        # An advertisement has no agreed dates; the chat contract is independent.
        contract = tbl_booking.objects.create(poster_id=self.owner, recipient_id=self.worker, **self.payload())
        contract.refresh_from_db()
        accept_engagement(contract.pk, self.worker)
        response = self.client.get(reverse('booking-detail', kwargs={'booking_id': contract.pk}))
        self.assertEqual(response.data['contract_summary']['work_setup'], 'stay_out')
        row.refresh_from_db()
        self.assertEqual(row.booking_status, 'Pending')


class WorkSetupMigrationTests(TransactionTestCase):
    def _fixture_teardown(self):
        if connection.vendor != 'postgresql':
            return super()._fixture_teardown()
        if (not connection.settings_dict['NAME'].startswith('test_')
                or connection.settings_dict['HOST'] not in ('localhost', '127.0.0.1', '::1')):
            raise RuntimeError('Audit fixture cleanup requires an isolated local test database.')
        tables = connection.introspection.table_names()
        protected = [(table, trigger) for table, trigger in (
            ('tbl_account_access_audit', 'account_access_audit_immutable'),
            ('tbl_penalty_adjustment', 'penalty_adjustment_immutable'),
        ) if table in tables]
        with connection.cursor() as cursor:
            for table, trigger in protected:
                cursor.execute(f'ALTER TABLE {table} DISABLE TRIGGER {trigger}')
        try:
            return super()._fixture_teardown()
        finally:
            with connection.cursor() as cursor:
                for table, trigger in protected:
                    cursor.execute(f'ALTER TABLE {table} ENABLE TRIGGER {trigger}')

    def test_backfill_uses_only_unambiguous_opening_setup_not_duration_or_instructions(self):
        owner = User.objects.create_user(email='setup-migration@test.com', username='setup-migration', password='test', account_type='Homeowner')
        previous = [('booking', '0012_explicit_compensation_and_transitions')]
        target = [('booking', '0014_tbl_booking_daily_end_time_and_more'), ('chat', '0006_tbl_chat_message_booking_proposal_and_more')]
        from verifications.testing import prepare_legacy_schema_test
        prepare_legacy_schema_test()
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        if (not connection.settings_dict['NAME'].startswith('test_')
                or connection.settings_dict['HOST'] not in ('localhost', '127.0.0.1', '::1')):
            raise RuntimeError('Migration fixtures require an isolated local test database.')
        executor.migrate(previous)
        retained_points_column = False
        try:
            # Migration 0015 deliberately retains this physical column on rollback.
            # Historical inserts need a temporary default in this disposable DB.
            if connection.vendor == 'postgresql':
                with connection.cursor() as cursor:
                    retained_points_column = any(column.name == 'cancellation_penalty_points'
                        for column in connection.introspection.get_table_description(cursor, 'tbl_booking'))
                    if retained_points_column:
                        cursor.execute('ALTER TABLE tbl_booking ALTER COLUMN cancellation_penalty_points SET DEFAULT 0')
            OldBooking = executor.loader.project_state(previous).apps.get_model('booking', 'tbl_booking')
            examples = [
                ('Looking for long-term work on a stay-out arrangement.', 'stay_out'),
                ('Looking for help on a stay-in setup.\n\nSpecific instructions:\nStay-out setup mentioned here.', 'stay_in'),
                ('Flexible setup.\n\nSpecific instructions:\nA stay-in setup may be discussed.', None),
                ('Flexible work.', None),
                ('A stay-in setup or stay-out arrangement.', None),
            ]
            rows = []
            for text, expected in examples:
                row = OldBooking.objects.create(poster_id_id=owner.pk, booking_type='long_term', rate_amount='8000', rate_period='month',
                    service_category=['Cleaning'], start_time=timezone.now()+timedelta(days=2), special_instruction=text)
                rows.append((row.pk, expected, text))
            MigrationExecutor(connection).migrate(target)
            for pk, expected, text in rows:
                row = tbl_booking.objects.get(pk=pk)
                self.assertEqual(row.work_setup, expected)
                self.assertEqual(row.special_instruction, text)
        finally:
            if retained_points_column:
                with connection.cursor() as cursor:
                    cursor.execute('ALTER TABLE tbl_booking ALTER COLUMN cancellation_penalty_points DROP DEFAULT')
            MigrationExecutor(connection).migrate(latest)
