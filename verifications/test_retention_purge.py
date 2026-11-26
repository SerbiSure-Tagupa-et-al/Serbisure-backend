import json
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from verifications.models import tbl_documents as Document, tbl_audit_logs as Audit
from verifications.serializers import DocumentUploadSerializer, AdminVerificationQueueSerializer
from verifications.serializers_admin import UserDocumentStatusSerializer
from verifications.services.document_processor import document_needs_retry
from verifications.services.media_retention import retire_media
from verifications.services.profile_photos import check_photo_upload


@override_settings(VERIFICATION_MEDIA_RETENTION_START_AT='2026-11-25T00:00:00+08:00')
class RetentionPurgeTests(TestCase):
    def setUp(self):
        self.now = datetime(2027, 2, 1, 12, tzinfo=dt_timezone.utc)
        self.clock = patch('verifications.services.media_retention.timezone.now', return_value=self.now).start()
        self.destroy = patch('cloudinary.uploader.destroy', return_value={'result': 'ok'}).start()
        self.addCleanup(patch.stopall)
        self.user = get_user_model().objects.create_user(username='retention-owner', email='retention-owner@test.com',
            account_type='Homeowner', contact_number='+639111111111')
        get_user_model().objects.filter(pk=self.user.pk).update(date_joined=self.now - timedelta(days=400))

    def document(self, kind='nbi_clearance', status='Pending', age=0, **fields):
        doc = Document.objects.create(user_profile=self.user, document_type=kind, verification_status=status,
            document_url=f'serbisure_credentials/{kind}-{Document.objects.count()}', **fields)
        Document.objects.filter(pk=doc.pk).update(created_at=self.now - timedelta(days=age))
        doc.refresh_from_db()
        return doc

    def run_cleanup(self, *args):
        output = StringIO()
        call_command('purge_expired_verification_media', *args, stdout=output, stderr=StringIO())
        return json.loads(output.getvalue().splitlines()[-1])

    def test_dry_run_reports_without_cloud_or_database_writes(self):
        doc = self.document(status='Rejected', age=40)
        before = Audit.objects.count()
        report = self.run_cleanup('--dry-run')
        self.assertEqual(report['eligible_by_category'], {'rejected': 1})
        self.assertEqual(report['purged_by_category'], {})
        self.destroy.assert_not_called()
        doc.refresh_from_db()
        self.assertTrue(doc.document_url)
        self.assertIsNone(doc.media_purged_at)
        self.assertEqual(Audit.objects.count(), before)
        self.assertFalse(Audit.objects.filter(metadata__reason_code='retention_cron_run').exists())

    def test_live_runs_record_one_summary_each_even_when_no_documents_are_due(self):
        self.document(age=1)
        for run in range(1, 3):
            report = self.run_cleanup()
            events = Audit.objects.filter(metadata__reason_code='retention_cron_run')
            self.assertEqual(events.count(), run)
            event = events.first()
            self.assertEqual(event.action, 'UPDATED')
            self.assertEqual(event.actor_name, 'System / Automated')
            self.assertEqual(event.actor_role, 'System')
            self.assertEqual(event.target_name, 'All Verification Media (RA 10173)')
            self.assertEqual(event.document_type, 'Retention Policy')
            self.assertEqual(event.previous_status, 'Scheduled')
            self.assertEqual(event.new_status, 'Completed')
            self.assertEqual(event.reason, 'Daily RA 10173 retention check: 0 purged, 1 checked, 0 overdue pending, 0 errors.')
            self.assertEqual(event.metadata, {'reason_code': 'retention_cron_run', **{key: value for key, value in report.items() if key != 'dry_run'}})
        self.destroy.assert_not_called()

    def test_empty_database_live_run_still_records_a_zero_count_summary(self):
        self.run_cleanup()
        event = Audit.objects.get(metadata__reason_code='retention_cron_run')
        self.assertEqual(event.metadata['eligible_by_category'], {})
        self.assertEqual(event.metadata['purged_by_category'], {})
        self.assertEqual(event.metadata['skipped'], 0)
        self.assertEqual(event.metadata['errors'], 0)
        self.assertEqual(event.reason, 'Daily RA 10173 retention check: 0 purged, 0 checked, 0 overdue pending, 0 errors.')

    def test_live_summary_records_purged_skipped_and_overdue_counts(self):
        self.document(status='Rejected', age=40)
        self.document(kind='police_clearance', age=35)
        report = self.run_cleanup()
        event = Audit.objects.get(metadata__reason_code='retention_cron_run')
        self.assertEqual(event.metadata['eligible_by_category'], {'rejected': 1})
        self.assertEqual(event.metadata['purged_by_category'], {'rejected': 1})
        self.assertEqual(event.metadata['overdue_pending_flagged'], 1)
        self.assertEqual(event.metadata['skipped'], 1)
        self.assertEqual(event.reason, 'Daily RA 10173 retention check: 1 purged, 2 checked, 1 overdue pending, 0 errors.')
        self.assertEqual(event.metadata['retention_start_at'], report['retention_start_at'])

    def test_queue_exposes_purge_dates_and_preserves_status_reviewer_and_dates(self):
        reviewer = get_user_model().objects.create_user(username='purge-reviewer', email='purge-reviewer@test.com',
            account_type='Admin', contact_number='+639333333333')
        doc = self.document(status='Verified', age=40, verifyBy=reviewer,
            date_issued=self.now.date() - timedelta(days=100), valid_until=self.now.date() + timedelta(days=100))
        before = AdminVerificationQueueSerializer(doc).data
        self.assertIsNone(before['mediaPurgedAt'])
        self.assertIsNone(before['secondaryMediaPurgedAt'])
        self.run_cleanup()
        doc.refresh_from_db()
        after = AdminVerificationQueueSerializer(doc).data
        self.assertEqual(datetime.fromisoformat(after['mediaPurgedAt'].replace('Z', '+00:00')), self.now)
        self.assertIsNone(after['documentImage'])
        self.assertIsNone(after['secondaryDocumentImage'])
        self.assertEqual(doc.verifyBy_id, reviewer.pk)
        for field in ('status', 'primaryStatus', 'issuedDate', 'validityDate'):
            self.assertEqual(after[field], before[field])

    def test_companion_purge_timestamp_and_null_images_are_serialized_independently(self):
        primary = self.document(kind='national_id_front', age=1)
        companion = self.document(kind='national_id_back', age=1, media_purged_at=self.now)
        with patch.object(AdminVerificationQueueSerializer, '_get_companion_doc', return_value=companion):
            data = AdminVerificationQueueSerializer(primary).data
        self.assertIsNone(data['mediaPurgedAt'])
        self.assertTrue(data['documentImage'])
        self.assertEqual(datetime.fromisoformat(data['secondaryMediaPurgedAt'].replace('Z', '+00:00')), self.now)
        self.assertIsNone(data['secondaryDocumentImage'])
        self.assertIsNone(data['documentImageBack'])

    def test_rejection_boundary_and_flags_do_not_extend_retention(self):
        old = self.document(status='Rejected', age=30, ocr_discrepancies=[{'field': 'image_quality', 'severity': 'high'}])
        recent = self.document(kind='police_clearance', status='Rejected', age=10)
        report = self.run_cleanup()
        self.assertEqual(report['purged_by_category'], {'rejected': 1})
        old.refresh_from_db(); recent.refresh_from_db()
        self.assertEqual(old.document_url, '')
        self.assertEqual(old.media_purged_at, self.now)
        self.assertTrue(recent.document_url)
        self.assertEqual(old.verification_status, 'Rejected')
        self.destroy.assert_called_once_with('serbisure_credentials/nbi_clearance-0', type='authenticated', resource_type='image', invalidate=True, timeout=10)

    def test_approved_images_are_purged_without_losing_verified_account_or_dates(self):
        date_issued, valid_until = self.now.date() - timedelta(days=100), self.now.date() + timedelta(days=200)
        kinds = ['national_id_front', 'national_id_back', 'nbi_clearance', 'police_clearance', 'tesda_nc2']
        documents = [self.document(kind=kind, status='Verified', age=31, date_issued=date_issued,
            valid_until=valid_until, extracted_data={'full_name': 'Private OCR Name'}, ocr_raw_text='Private raw OCR') for kind in kinds]
        avatar = self.document(kind='profile_photo', status='Verified', age=100)
        self.assertEqual(self.user.verification_status, 'Verified')
        report = self.run_cleanup()
        self.assertEqual(report['purged_by_category'], {'verified': len(kinds)})
        for doc in documents:
            doc.refresh_from_db()
            self.assertEqual(doc.verification_status, 'Verified')
            self.assertEqual(doc.date_issued, date_issued)
            self.assertEqual(doc.valid_until, valid_until)
            self.assertEqual(doc.document_url, '')
            self.assertEqual(doc.extracted_data, {})
            self.assertIsNone(doc.ocr_raw_text)
            self.assertIsNone(DocumentUploadSerializer(doc).data['document_url'])
            self.assertIsNone(AdminVerificationQueueSerializer(doc).data['documentImage'])
            self.assertIsNone(UserDocumentStatusSerializer(doc).data['document_image_url'])
            self.assertFalse(document_needs_retry(doc))
        self.user.refresh_from_db(); avatar.refresh_from_db()
        self.assertEqual(self.user.verification_status, 'Verified')
        self.assertTrue(avatar.document_url)
        self.assertEqual(Audit.objects.filter(metadata__reason_code='retention_media_purged').count(), len(kinds))

    def test_latest_approval_starts_the_clock_not_the_upload_date(self):
        doc = self.document(status='Verified', age=100)
        event = Audit.objects.create(document=doc, action='APPROVED', new_status='Verified')
        Audit.objects.filter(pk=event.pk).update(created_at=self.now - timedelta(days=10))
        self.run_cleanup()
        self.destroy.assert_not_called()
        doc.refresh_from_db()
        self.assertTrue(doc.document_url)

    def test_pending_is_flagged_at_35_days_and_abandoned_at_65(self):
        pending = self.document(age=35)
        abandoned = self.document(kind='tesda_nc2', age=65)
        report = self.run_cleanup()
        self.assertEqual(report['overdue_pending_flagged'], 1)
        self.assertEqual(report['purged_by_category'], {'abandoned_pending': 1})
        pending.refresh_from_db(); abandoned.refresh_from_db()
        self.assertTrue(pending.document_url)
        self.assertEqual(pending.verification_status, 'Pending')
        self.assertTrue(any(flag['field'] == 'review_overdue' for flag in AdminVerificationQueueSerializer(pending).data['ocrDiscrepancies']))
        self.assertEqual(abandoned.document_url, '')
        self.assertEqual(abandoned.verification_status, 'Unverified')
        self.assertTrue(UserDocumentStatusSerializer(abandoned).data['can_replace'])

    def test_active_profile_photo_is_never_purged_by_age(self):
        doc = self.document(kind='profile_photo', status='Rejected', age=365)
        self.run_cleanup()
        self.destroy.assert_not_called()
        doc.refresh_from_db()
        self.assertTrue(doc.document_url)

    def test_account_closure_purges_all_images_after_closure_grace_period(self):
        photo = self.document(kind='profile_photo', status='Verified', age=100)
        credential = self.document(status='Verified', age=10)
        self.user.is_active = False
        self.user.save()
        self.run_cleanup()
        self.destroy.assert_not_called()
        closure = Audit.objects.get(metadata__reason_code='retention_account_closed', target_user=self.user)
        self.clock.return_value = self.now + timedelta(days=31)
        report = self.run_cleanup()
        self.assertEqual(report['purged_by_category'], {'closed_account': 2})
        photo.refresh_from_db(); credential.refresh_from_db(); self.user.refresh_from_db()
        self.assertEqual(photo.document_url, '')
        self.assertEqual(credential.document_url, '')
        self.assertEqual(self.user.profile_link, '')
        self.user.is_active = True
        self.user.save()
        check_photo_upload(self.user)  # Restored accounts can upload a new avatar.

    def test_closure_does_not_extend_an_earlier_approval_deadline(self):
        doc = self.document(status='Verified', age=31)
        self.user.is_active = False
        self.user.save()
        report = self.run_cleanup()
        self.assertEqual(report['purged_by_category'], {'verified': 1})
        doc.refresh_from_db()
        self.assertEqual(doc.document_url, '')

    def test_replacement_clock_uses_the_new_submission_date(self):
        old = self.document(age=55)
        current = self.document(age=40)
        report = self.run_cleanup()
        self.assertEqual(report['purged_by_category'], {'replaced': 1})
        old.refresh_from_db(); current.refresh_from_db()
        self.assertEqual(old.document_url, '')
        self.assertTrue(current.document_url)

    def test_retired_in_place_image_is_tracked_and_purged_once(self):
        doc = self.document(status='Rejected', age=40)
        old_url = doc.document_url
        event = retire_media(doc, 'replaced')
        original_metadata = dict(event.metadata)
        Document.objects.filter(pk=doc.pk).update(document_url='serbisure_credentials/new',
            verification_status='Pending', created_at=self.now)
        report = self.run_cleanup()
        self.assertEqual(report['purged_by_category'], {'retired': 1})
        self.destroy.assert_called_once_with(old_url, type='authenticated', resource_type='image', invalidate=True, timeout=10)
        self.run_cleanup()
        self.assertEqual(self.destroy.call_count, 1)
        event.refresh_from_db(); doc.refresh_from_db()
        self.assertEqual(event.metadata, original_metadata)
        self.assertEqual(doc.document_url, 'serbisure_credentials/new')

    def test_expiry_clock_is_independent_of_approval(self):
        doc = self.document(status='Verified', age=40, valid_until=self.now.date() - timedelta(days=32))
        approval = Audit.objects.create(document=doc, action='APPROVED')
        report = self.run_cleanup()
        self.assertEqual(report['purged_by_category'], {'expired': 1})
        approval.refresh_from_db()
        self.assertEqual(approval.action, 'APPROVED')

    def test_deleted_row_keeps_the_recent_rejection_clock_in_its_retirement_event(self):
        doc = self.document(status='Rejected', age=100)
        Audit.objects.create(document=doc, action='REJECTED', new_status='Rejected')
        doc.delete()
        self.run_cleanup()
        self.destroy.assert_not_called()
        self.clock.return_value = self.now + timedelta(days=30)
        self.run_cleanup()
        self.destroy.assert_called_once()

    def test_cloud_failure_does_not_clear_the_reference_or_claim_success(self):
        doc = self.document(status='Rejected', age=40)
        self.destroy.side_effect = RuntimeError('offline')
        with self.assertRaises(CommandError):
            self.run_cleanup()
        doc.refresh_from_db()
        self.assertTrue(doc.document_url)
        self.assertIsNone(doc.media_purged_at)
        self.assertFalse(Audit.objects.filter(metadata__reason_code='retention_media_purged').exists())
        event = Audit.objects.get(metadata__reason_code='retention_cron_run')
        self.assertEqual(event.new_status, 'Completed with Errors')
        self.assertEqual(event.metadata['eligible_by_category'], {'rejected': 1})
        self.assertEqual(event.metadata['purged_by_category'], {})
        self.assertEqual(event.metadata['errors'], 1)
        self.assertEqual(event.reason, 'Daily RA 10173 retention check: 0 purged, 1 checked, 0 overdue pending, 1 errors.')

    def test_shared_active_avatar_cannot_be_destroyed_by_another_document(self):
        doc = self.document(status='Rejected', age=40)
        get_user_model().objects.filter(pk=self.user.pk).update(profile_link=doc.document_url)
        report = self.run_cleanup()
        self.assertEqual(report['purged_by_category'], {})
        self.destroy.assert_not_called()

    @patch('verifications.services.media_retention.cloudinary.config', return_value=SimpleNamespace(cloud_name='test-cloud'))
    def test_signed_url_alias_of_a_current_avatar_is_also_protected(self, _config):
        doc = self.document(status='Rejected', age=40)
        Document.objects.filter(pk=doc.pk).update(document_url='https://res.cloudinary.com/test-cloud/image/authenticated/v123/current-avatar.jpg')
        get_user_model().objects.filter(pk=self.user.pk).update(profile_link='current-avatar')
        self.run_cleanup()
        self.destroy.assert_not_called()
        get_user_model().objects.filter(pk=self.user.pk).update(profile_link='')
        other = self.document(kind='police_clearance')
        Document.objects.filter(pk=other.pk).update(document_url='current-avatar')
        self.run_cleanup()
        self.destroy.assert_not_called()

    def test_purged_image_cannot_be_reviewed_again_or_reprocessed(self):
        doc = self.document(status='Verified', age=40)
        self.run_cleanup()
        reviewer = get_user_model().objects.create_user(username='retention-admin', email='retention-admin@test.com',
            account_type='Admin', contact_number='+639222222222')
        client = APIClient(); client.force_authenticate(reviewer)
        with patch('verifications.views_admin.process_document_async') as process:
            response = client.patch(reverse('admin-document-action', args=[doc.pk]), {'verification_status': 'Verified'}, format='json')
            self.assertEqual(response.status_code, 400)
            response = client.post(reverse('admin-document-reprocess', args=[doc.pk]))
            self.assertEqual(response.status_code, 400)
            response = client.post(reverse('admin-verification-review', args=[doc.pk]), {'action': 'reset'}, format='json')
            self.assertEqual(response.status_code, 400)
            process.assert_not_called()

    def test_existing_images_get_full_30_days_from_november_25(self):
        doc = self.document(status='Rejected', age=365)
        self.clock.return_value = datetime(2026, 12, 24, 15, 59, tzinfo=dt_timezone.utc)
        self.run_cleanup()
        self.destroy.assert_not_called()
        doc.refresh_from_db()
        self.assertTrue(doc.document_url)
        self.clock.return_value = datetime(2026, 12, 24, 16, tzinfo=dt_timezone.utc)  # Dec 25, midnight PH
        self.run_cleanup()
        self.destroy.assert_called_once()

    def test_later_rejection_receives_its_own_full_period(self):
        doc = self.document(status='Rejected', age=365)
        event = Audit.objects.create(document=doc, action='REJECTED')
        self.run_cleanup()
        self.destroy.assert_not_called()
        self.assertEqual(event.created_at, self.now)

    def test_options_cannot_shorten_the_agreed_grace_period(self):
        with self.assertRaises(CommandError):
            self.run_cleanup('--days', '1')
