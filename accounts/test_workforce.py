from datetime import datetime, timedelta
from unittest.mock import patch
from django.urls import reverse
from django.db import transaction
from rest_framework.test import APITestCase
from .test_admin_statistics import AdminStatisticsTests
from .models import tbl_user_profile as User, tbl_worker_profile, tbl_worker_status_history as History
from .workforce import PH_TIME
from verifications.models import tbl_documents as Document


class WorkforceTests(APITestCase):
    """Real document/status writes, month-end boundaries, jurisdiction and profile skills."""
    setUp = AdminStatisticsTests.setUp
    user = AdminStatisticsTests.user
    def test_partial_rejected_pending_expired_and_replaced_clearances_are_not_verified(self):
        for kind in ('nbi_clearance', 'police_clearance'):
            Document.objects.create(user_profile=self.worker, document_type=kind, document_url='replacement', verification_status='Pending')
        self.assertEqual(self.client.get(self.stats).data['metrics']['totalWorkers'], 0)
        for status in ('Unverified', 'Rejected', 'Verified'):
            worker = self.user('Kasambahay', 'Pagatpat', is_on_job=True)
            Document.objects.create(user_profile=worker, document_type='profile_photo', document_url='test/face', verification_status='Verified')
            Document.objects.create(user_profile=worker, document_type='nbi_clearance', document_url='nbi', verification_status='Verified')
            if status != 'Unverified':
                Document.objects.create(user_profile=worker, document_type='police_clearance', document_url='police', verification_status=status)
            expected = 1 if status == 'Verified' else 0
            self.assertEqual(self.client.get(self.stats).data['metrics']['totalWorkers'], expected)
        doc = worker.documents.get(document_type='police_clearance')
        doc.valid_until = datetime.now(PH_TIME).date() - timedelta(days=1); doc.save()
        self.assertEqual(self.client.get(self.stats).data['metrics']['totalWorkers'], 0)

    def test_registration_verification_toggle_and_rollback_are_atomic(self):
        worker = self.user('Kasambahay', 'Agusan')
        entries = History.objects.filter(worker=worker)
        self.assertEqual(entries.count(), 1)
        self.assertFalse(entries.get().status)
        for kind in ('nbi_clearance', 'police_clearance', 'profile_photo'):
            Document.objects.create(user_profile=worker, document_type=kind, document_url='clearance', verification_status='Verified')
        self.assertEqual(entries.count(), 2)
        self.assertTrue(entries.latest('changed_at').is_verified)
        self.client.force_authenticate(worker)
        for value in (True, True, False):
            result = self.client.patch(reverse('job-status'), {'is_on_job': value}, format='json')
            self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(entries.count(), 4)  # Repeated same-value request is not a change.
        self.assertFalse(entries.latest('changed_at').status)
        with self.assertRaises(RuntimeError), transaction.atomic():
            worker.is_on_job = True; worker.save(update_fields=['is_on_job'])
            raise RuntimeError('rollback')
        worker.refresh_from_db(); self.assertFalse(worker.is_on_job)
        self.assertEqual(entries.count(), 4)
        with patch('accounts.workforce.record_worker_status', side_effect=RuntimeError('history unavailable')):
            with self.assertRaises(RuntimeError):
                self.client.patch(reverse('job-status'), {'is_on_job': True}, format='json')
        worker.refresh_from_db(); self.assertFalse(worker.is_on_job)
        self.assertEqual(entries.count(), 4)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.patch(reverse('job-status'), {'is_on_job': True}, format='json').status_code, 403)

    def test_skills_count_unique_verified_profiles_and_follow_barangay(self):
        tbl_worker_profile.objects.create(user=self.worker, service_categories=['Cleaning', 'Cooking', 'Cleaning'])
        other = self.user('Kasambahay', 'Agusan', verification_status='Verified')
        tbl_worker_profile.objects.create(user=other, service_categories=['All-around'])
        unverified = self.user('Kasambahay', 'Pagatpat')
        tbl_worker_profile.objects.create(user=unverified, service_categories=['Cooking'])
        city = self.client.get(self.stats).data['skills']
        self.assertEqual(city['totalWorkers'], 2)
        cleaning = next(row for row in city['categories'] if row['category'] == 'Cleaning')
        self.assertEqual(cleaning, {'category': 'Cleaning', 'count': 1, 'percentage': 50.0})
        local = self.client.get(self.stats, {'barangay': 'Pagatpat'}).data['skills']
        self.assertEqual(local['totalWorkers'], 1)
        self.assertEqual(next(row for row in local['categories'] if row['category'] == 'Cooking')['percentage'], 100)

    def test_month_end_uses_latest_event_at_ph_midnight_and_preserves_old_scope_and_verification(self):
        worker_id = self.worker.pk
        History.objects.all().delete()
        def event(at, **values):
            row = History.objects.create(worker=self.worker, worker_id_snapshot=worker_id, status=False, barangay='Pagatpat', is_verified=True)
            History.objects.filter(pk=row.pk).update(changed_at=datetime.fromisoformat(at).replace(tzinfo=PH_TIME), **values)
        event('2026-07-01T00:00:00', is_verified=False)
        event('2026-08-01T00:00:00')
        event('2026-08-31T23:59:59.999999', status=True)
        event('2026-09-01T00:00:00', status=False, barangay='Agusan')
        event('2026-10-01T00:00:00', is_verified=False)
        self.worker.barangay = 'Agusan'; self.worker.is_on_job = False
        self.worker.save(update_fields=['barangay', 'is_on_job'])
        url = reverse('admin-monthly-trend')
        with patch('accounts.workforce.timezone.now', return_value=datetime(2026, 10, 19, tzinfo=PH_TIME)):
            city = self.client.get(url).data
            self.assertFalse(city['trend'][0]['has_history'])
            self.assertIsNone(city['trend'][0]['employed'])
            self.assertEqual(city['trend'][-3]['employed'], 1)  # August: exact last microsecond
            self.assertEqual(city['trend'][-2]['available'], 1)  # September: new address
            self.assertEqual(city['trend'][-1]['available'], 1)  # October: live documents + toggle
            local = self.client.get(url, {'barangay': 'Pagatpat'}).data
            self.assertEqual(local['trend'][-3]['employed'], 1)
            self.assertEqual(local['trend'][-2]['total'], 0)
            self.assertEqual(local['trend'][-1]['total'], 0)

    def test_document_revocation_and_account_deletion_keep_history(self):
        old_id = self.worker.pk
        self.worker.documents.get(document_type='police_clearance').delete()
        self.assertFalse(History.objects.filter(worker=self.worker).latest('changed_at').is_verified)
        self.worker.delete()
        rows = History.objects.filter(worker_id_snapshot=old_id)
        self.assertGreater(rows.count(), 1)
        self.assertFalse(rows.latest('changed_at').is_verified)
        self.assertFalse(rows.exclude(worker=None).exists())

    def test_role_filters_require_complete_credentials_and_officers_cannot_escape_scope(self):
        url = reverse('admin-verification-status-stats')
        for role, count in (('All Roles', 2), ('Kasambahay', 1), ('Homeowner', 1)):
            data = self.client.get(url, {'role': role}).data
            self.assertEqual(data['total'], count)
            self.assertEqual(data['stats']['verified'], 0 if role == 'Homeowner' else 1)
        self.assertEqual(self.client.get(url, {'role': 'Admin'}).status_code, 400)
        officer = self.user('Barangay', 'Agusan')
        self.client.force_authenticate(officer)
        self.assertEqual(self.client.get(url, {'barangay': 'All Barangays'}).data['total'], 0)
        self.assertEqual(self.client.get(reverse('admin-monthly-trend'), {'barangay': 'Pagatpat'}).data['total_workers'], 0)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(reverse('admin-monthly-trend')).status_code, 401)

    def test_expired_clearance_history_is_evaluated_at_the_month_end_not_today(self):
        History.objects.all().delete()
        row = History.objects.create(worker=self.worker, worker_id_snapshot=self.worker.pk, status=True, barangay='Pagatpat',
                                     is_verified=True, clearance_valid_until=datetime(2026, 8, 31).date())
        History.objects.filter(pk=row.pk).update(changed_at=datetime(2026, 8, 1, tzinfo=PH_TIME))
        with patch('accounts.workforce.timezone.now', return_value=datetime(2026, 10, 19, tzinfo=PH_TIME)):
            points = self.client.get(reverse('admin-monthly-trend')).data['trend']
        self.assertEqual(points[-3]['employed'], 1)
        self.assertEqual(points[-2]['total'], 0)

    def test_baseline_migration_records_current_values_without_backdating(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        from django.utils import timezone
        migration = importlib.import_module('accounts.migrations.0023_worker_status_history')
        History.objects.all().delete()
        before = timezone.now()
        with connection.schema_editor() as editor:
            migration.seed_current_status(apps, editor)
        row = History.objects.get(worker=self.worker)
        self.assertGreaterEqual(row.changed_at, before)
        self.assertEqual(row.status, self.worker.is_on_job)
        self.assertTrue(row.is_verified)
