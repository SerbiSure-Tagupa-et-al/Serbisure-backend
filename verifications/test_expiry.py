from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from accounts.models import tbl_user_profile as User
from accounts.workforce import PH_TIME, credential_snapshot
from booking.services import ensure_eligible_pair
from notifications.models import tbl_notification
from verifications.models import tbl_documents as Document, tbl_audit_logs as Audit
from verifications.test_document_processing import image_file
from verifications.testing import AuditTransactionTestCase


class ClearanceExpiryTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.sequence = 0
        self.today = timezone.localdate(timezone=PH_TIME)
        self.admin = self.user('Admin')
        self.desk = self.user('Barangay', 'Brgy. Pagatpat')
        self.worker = self.user('Kasambahay', 'Pagatpat')
        self.other = self.user('Kasambahay', 'Agusan')
        self.nbi = self.document(self.worker, 'nbi_clearance', -1)
        self.police = self.document(self.worker, 'police_clearance', 30)
        self.foreign = self.document(self.other, 'police_clearance', -2)
        self.url = reverse('admin-expired-clearances')
        self.queue = reverse('admin-verification-queue')
        self.stats = reverse('admin-verification-status-stats')
        self.client.force_authenticate(self.admin)

    def user(self, role, barangay=''):
        self.sequence += 1
        return User.objects.create_user(username=f'expiry-{self.sequence}', email=f'expiry-{self.sequence}@test.com',
                                        account_type=role, barangay=barangay)

    def document(self, user, kind, days, status='Verified'):
        return Document.objects.create(user_profile=user, document_type=kind, document_url=f'expiry-{kind}',
                                       verification_status=status, valid_until=self.today + timedelta(days=days),
                                       ocr_processed_at=timezone.now(), extracted_data={'first_name': 'Test'}, ocr_raw_text='complete')

    def preview(self, actor=None):
        if actor:
            self.client.force_authenticate(actor)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def confirm(self, preview):
        return self.client.post(self.url, {'confirmation_token': preview['confirmation_token']}, format='json')

    def test_reads_report_expiry_but_never_reject_or_notify(self):
        before = tbl_notification.objects.count()
        preview = self.preview()
        self.assertEqual((preview['document_count'], preview['worker_count']), (2, 2))
        stats = self.client.get(self.stats).data
        self.assertEqual(stats['stats']['expired'], 2)
        queue = self.client.get(self.queue).data
        self.assertEqual({row['applicantStatus'] for row in queue}, {'EXPIRED'})
        self.nbi.refresh_from_db()
        self.assertEqual(self.nbi.verification_status, 'Verified')
        self.assertEqual(tbl_notification.objects.count(), before)
        self.assertFalse(Audit.objects.filter(metadata__manual_expiration_action=True).exists())

    def test_queue_and_dashboard_have_the_same_person_counts_for_every_role_and_scope(self):
        owner = self.user('Homeowner', 'Pagatpat')
        for kind in ('national_id_front', 'national_id_back'):
            self.document(owner, kind, 90)
        self.user('Kasambahay', 'Pagatpat Heights')
        self.document(self.worker, 'police_clearance', 90)
        for actor in (self.admin, self.desk):
            self.client.force_authenticate(actor)
            for role in ('All Roles', 'Kasambahay', 'Homeowner'):
                for scope in ('All Barangays', 'Pagatpat', 'UNASSIGNED'):
                    params = {'role': role, 'barangay': scope}
                    stats = self.client.get(self.stats, params).data
                    queue = self.client.get(self.queue, params).data
                    applicants = {row['userId']: row['applicantStatus'] for row in queue}
                    mapping = {'VERIFIED': 'verified', 'REJECTED': 'rejected', 'EXPIRED': 'expired',
                               'PENDING / REVIEW': 'pending', 'NO_DOCUMENTS': 'unverified'}
                    self.assertEqual(len(applicants), stats['total'])
                    for portal, key in mapping.items():
                        self.assertEqual(sum(value == portal for value in applicants.values()), stats['stats'][key])

    def test_barangay_action_only_rejects_its_expired_clearance_and_not_its_companion(self):
        preview = self.preview(self.desk)
        self.assertEqual(preview['document_count'], 1)
        # The caller cannot expand the jurisdiction using query strings.
        self.assertEqual(self.client.get(self.url, {'barangay': 'Agusan'}).data['document_count'], 1)
        result = self.confirm(preview)
        self.assertEqual(result.status_code, 200, result.data)
        for doc in (self.nbi, self.police, self.foreign):
            doc.refresh_from_db()
        self.assertEqual(self.nbi.verification_status, 'Rejected')
        self.assertEqual(self.nbi.verifyBy, self.desk)
        self.assertIn(str(self.nbi.valid_until.year), self.nbi.rejection_reason)
        self.assertIn('renewed clearance', self.nbi.rejection_reason)
        self.assertEqual(self.police.verification_status, 'Verified')
        self.assertEqual(self.foreign.verification_status, 'Verified')
        audit = Audit.objects.get(document=self.nbi, action='REJECTED')
        self.assertEqual(audit.actor, self.desk)
        self.assertTrue(audit.metadata['manual_expiration_action'])
        self.assertTrue(tbl_notification.objects.filter(receiver_id=self.worker, notification_message=self.nbi.rejection_reason).exists())
        self.assertEqual(self.preview()['document_count'], 0)

    def test_superadmin_can_process_all_barangays_and_repeat_cannot_duplicate_side_effects(self):
        preview = self.preview()
        response = self.confirm(preview)
        self.assertEqual(response.data['document_count'], 2)
        count = Audit.objects.count(), tbl_notification.objects.count()
        self.assertEqual(self.confirm(preview).status_code, 409)
        self.assertEqual((Audit.objects.count(), tbl_notification.objects.count()), count)

    def test_confirmation_is_required_signed_actor_bound_and_expires(self):
        preview = self.preview()
        self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 400)
        self.assertEqual(self.confirm({'confirmation_token': 'tampered'}).status_code, 400)
        self.client.force_authenticate(self.desk)
        self.assertEqual(self.confirm(preview).status_code, 403)
        own = self.preview()
        with patch('django.core.signing.time.time', return_value=timezone.now().timestamp() + 301):
            self.assertEqual(self.confirm(own).status_code, 400)
        self.assertEqual(self.client.post(self.url, {'confirmation_token': own['confirmation_token'], 'barangay': 'Agusan'}, format='json').status_code, 400)

    def test_authentication_role_and_empty_jurisdiction_are_enforced(self):
        for actor, expected in [(self.worker, 403), (self.user('Barangay'), 403), (None, 401)]:
            self.client.force_authenticate(actor)
            self.assertEqual(self.client.get(self.url).status_code, expected)
            self.assertEqual(self.client.post(self.url, {}, format='json').status_code, expected)

    def test_preview_changes_require_new_confirmation_instead_of_rejecting_a_renewal(self):
        preview = self.preview()
        self.nbi.valid_until = self.today + timedelta(days=90)
        self.nbi.save(update_fields=['valid_until'])
        self.assertEqual(self.confirm(preview).status_code, 409)
        self.nbi.refresh_from_db()
        self.foreign.refresh_from_db()
        self.assertEqual(self.nbi.verification_status, 'Verified')
        self.assertEqual(self.foreign.verification_status, 'Verified')

    def test_current_required_documents_only_and_no_homeowner_inactive_unknown_or_valid_dates(self):
        owner = self.user('Homeowner', 'Pagatpat')
        self.document(owner, 'national_id_front', -1)
        disabled = self.user('Kasambahay', 'Pagatpat')
        self.document(disabled, 'nbi_clearance', -1)
        disabled.is_active = False; disabled.save(update_fields=['is_active'])
        old = self.document(self.worker, 'police_clearance', -3)
        Document.objects.filter(pk=old.pk).update(created_at=timezone.now()-timedelta(days=7))
        missing = self.document(self.other, 'nbi_clearance', 1)
        missing.valid_until = None; missing.save(update_fields=['valid_until'])
        self.police.valid_until = self.today; self.police.save(update_fields=['valid_until'])
        self.assertEqual(self.preview()['document_count'], 2)

    def test_jurisdiction_change_invalidates_confirmation(self):
        preview = self.preview(self.desk)
        User.objects.filter(pk=self.worker.pk).update(barangay='Agusan')
        self.assertEqual(self.confirm(preview).status_code, 409)

    def test_notification_or_audit_failure_rolls_back_every_decision(self):
        for target, kwargs in [('verifications.services.expiry.tbl_notification.objects.create', {'side_effect': RuntimeError('offline')}),
                               ('verifications.services.expiry.record_audit_log', {'return_value': None})]:
            preview = self.preview()
            before = Audit.objects.count()
            self.client.raise_request_exception = False
            with patch(target, **kwargs):
                self.assertEqual(self.confirm(preview).status_code, 500)
            self.assertEqual(Audit.objects.count(), before)
            self.nbi.refresh_from_db()
            self.assertEqual(self.nbi.verification_status, 'Verified')

    def test_rejected_renewal_upload_preserves_valid_companion_and_waits_for_review(self):
        self.assertEqual(self.confirm(self.preview(self.desk)).status_code, 200)
        self.client.force_authenticate(self.worker)
        status = self.client.get('/api/v1/verifications/status/').data
        self.assertEqual(next(doc for doc in status['documents'] if doc['document_id'] == str(self.nbi.pk))['verification_status'], 'Rejected')
        with patch('cloudinary.uploader.upload', return_value={'public_id': 'renewed-nbi'}), \
             patch('verifications.services.document_processor.process_document_async'):
            response = self.client.post('/api/v1/verifications/upload/', {'document_type': 'nbi_clearance', 'document_image': image_file()}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        self.nbi.refresh_from_db(); self.police.refresh_from_db()
        self.assertEqual(self.nbi.verification_status, 'Pending')
        self.assertIsNone(self.nbi.valid_until)
        self.assertEqual(self.police.verification_status, 'Verified')
        self.worker.refresh_from_db()
        self.assertNotEqual(self.worker.verification_status, 'Verified')
        self.nbi.valid_until = self.today + timedelta(days=90)
        self.nbi.verification_status = 'Verified'; self.nbi.save()
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.verification_status, 'Verified')

    def test_expired_worker_cannot_post_or_accept_even_before_manual_processing(self):
        owner = self.user('Homeowner')
        for kind in ('national_id_front', 'national_id_back'):
            self.document(owner, kind, 90)
        with self.assertRaises(PermissionDenied):
            ensure_eligible_pair(owner, self.worker)
        self.client.force_authenticate(self.worker)
        response = self.client.post(reverse('booking-post'), {}, format='json')
        self.assertEqual(response.status_code, 403, response.data)
        self.assertEqual(response.data['code'], 'account_not_verified')

    def test_expiration_uses_philippine_midnight_and_expiry_day_is_still_valid(self):
        self.nbi.valid_until = datetime(2026, 10, 29).date(); self.nbi.save(update_fields=['valid_until'])
        self.police.valid_until = datetime(2027, 1, 1).date(); self.police.save(update_fields=['valid_until'])
        for at, expected in [(datetime(2026, 10, 29, 15, 59, tzinfo=dt_timezone.utc), 'verified'),
                             (datetime(2026, 10, 29, 16, 0, tzinfo=dt_timezone.utc), 'expired')]:
            with patch('accounts.workforce.timezone.now', return_value=at):
                self.assertEqual(credential_snapshot(self.worker)[0], expected)


class ClearanceExpiryConcurrencyTests(AuditTransactionTestCase):

    @skipUnlessDBFeature('has_select_for_update')
    def test_two_officers_submitting_the_same_preview_cannot_duplicate_decisions_or_notifications(self):
        from rest_framework.test import APIClient
        admin = User.objects.create_user(email='expiry-race-admin@test.com', username='expiry-race-admin', account_type='Admin')
        worker = User.objects.create_user(email='expiry-race-worker@test.com', username='expiry-race-worker', account_type='Kasambahay')
        doc = Document.objects.create(user_profile=worker, document_type='nbi_clearance', document_url='race',
                                      verification_status='Verified',
                                      valid_until=timezone.localdate(timezone=PH_TIME)-timedelta(days=1))
        client = APIClient(); client.force_authenticate(admin)
        url = reverse('admin-expired-clearances')
        preview = client.get(url).data
        barrier = Barrier(2)
        def confirm():
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                actor = User.objects.get(pk=admin.pk)
                caller = APIClient(); caller.force_authenticate(actor)
                barrier.wait(timeout=10)
                return caller.post(url, {'confirmation_token': preview['confirmation_token']}, format='json').status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: confirm(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(Audit.objects.filter(document=doc, action='REJECTED').count(), 1)
        self.assertEqual(tbl_notification.objects.filter(receiver_id=worker, notification_message__contains='expired on').count(), 1)
