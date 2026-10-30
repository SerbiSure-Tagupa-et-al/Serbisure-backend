import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from io import BytesIO, StringIO
from threading import Barrier
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import close_old_connections, transaction
from django.test import skipUnlessDBFeature
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase

from accounts.models import tbl_user_profile
from verifications.models import tbl_documents
from verifications.testing import AuditTransactionTestCase
from verifications.services import document_processor as processor
from verifications.services.document_upload import create_uploaded_document


def make_user(role='Homeowner', suffix='owner'):
    return tbl_user_profile.objects.create_user(
        username=f'processing-{suffix}', email=f'processing-{suffix}@test.com',
        account_type=role, first_name='Maria', last_name='Santos',
    )


def image_file():
    stream = BytesIO()
    Image.new('RGB', (20, 20), 'white').save(stream, format='JPEG')
    return SimpleUploadedFile('document.jpg', stream.getvalue(), content_type='image/jpeg')


class AsyncUploadTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.client.force_authenticate(self.user)
        self.cloud = patch('cloudinary.uploader.upload', return_value={'public_id': 'new-image'}).start()
        self.thread = patch('verifications.services.document_processor.Thread').start()
        self.legacy = patch('verifications.ocr_service.process_document_ocr', side_effect=AssertionError('blocking OCR called')).start()
        self.fetch = patch('verifications.services.document_processor.fetch_image_from_cloudinary', side_effect=AssertionError('OCR ran inside upload')).start()
        self.addCleanup(patch.stopall)

    def upload(self, kind):
        return self.client.post('/api/v1/verifications/upload/', {
            'document_type': kind, 'document_image': image_file(),
        }, format='multipart')

    def test_both_roles_get_a_saved_pending_document_without_waiting_for_analysis(self):
        for role, kinds in [('Homeowner', ['national_id_front', 'national_id_back']),
                            ('Kasambahay', ['nbi_clearance', 'police_clearance'])]:
            self.user.account_type = role
            self.user.save(update_fields=['account_type'])
            for kind in kinds:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.upload(kind)
                self.assertEqual(response.status_code, 201, response.data)
                self.assertEqual(response.data['verification_status'], 'Pending')
                self.assertEqual(response.data['extracted_data'], {})
                self.assertIsNone(response.data['ocr_match_score'])
                document = tbl_documents.objects.get(pk=response.data['document_id'])
                self.assertEqual(document.document_url, 'new-image')
                self.assertIsNone(document.ocr_processed_at)
                self.assertIsNotNone(document.ocr_processing_token)
        self.assertEqual(self.cloud.call_count, 4)
        self.assertEqual(self.thread.return_value.start.call_count, 4)
        self.legacy.assert_not_called()
        self.fetch.assert_not_called()

    def test_rejected_replacement_clears_old_analysis_reviewer_and_retry_state(self):
        reviewer = make_user('Admin', 'reviewer')
        old_token = uuid.uuid4()
        document = tbl_documents.objects.create(
            user_profile=self.user, document_type='national_id_front', document_url='old-image',
            verification_status='Rejected', verifyBy=reviewer, rejection_reason='Old reason',
            extracted_data={'first_name': 'Old'}, document_number='old-number',
            date_issued=date(2020, 1, 1), valid_until=date(2021, 1, 1),
            ocr_raw_text='old OCR', ocr_match_score=40, ocr_discrepancies=[{'field': 'name'}],
            ocr_processed_at=timezone.now(), ocr_retry_count=3,
            ocr_processing_token=old_token, ocr_last_attempt_at=timezone.now(),
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = self.upload('national_id_front')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(str(document.pk), response.data['document_id'])
        document.refresh_from_db()
        for field in ['verifyBy_id', 'rejection_reason', 'document_number', 'date_issued',
                      'valid_until', 'ocr_raw_text', 'ocr_match_score', 'ocr_processed_at']:
            self.assertIsNone(getattr(document, field), field)
        self.assertEqual(document.extracted_data, {})
        self.assertEqual(document.ocr_discrepancies, [])
        self.assertEqual(document.ocr_retry_count, 0)
        self.assertNotEqual(document.ocr_processing_token, old_token)
        self.assertEqual(document.verification_status, 'Pending')
        self.assertEqual(document.document_url, 'new-image')

    def test_failed_thread_start_does_not_turn_a_saved_upload_into_an_error(self):
        self.thread.return_value.start.side_effect = RuntimeError('worker could not start')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.upload('national_id_front')
        self.assertEqual(response.status_code, 201)
        document = tbl_documents.objects.get(pk=response.data['document_id'])
        self.assertIsNone(document.ocr_processing_token)
        self.assertIsNone(document.ocr_processed_at)
        self.assertIsNotNone(document.ocr_last_attempt_at)


class DocumentProcessingTests(APITestCase):
    def setUp(self):
        self.user = make_user()
        self.reviewer = make_user('Admin', 'reviewer')
        self.document = tbl_documents.objects.create(
            user_profile=self.user, document_type='national_id_front', document_url='original-image',
        )
        self.client.force_authenticate(self.user)
        self.fetch = patch('verifications.services.document_processor.fetch_image_from_cloudinary', return_value=(Image.new('RGB', (20, 20)), b'image')).start()
        patch('verifications.services.document_processor.is_google_vision_available', return_value=True).start()
        self.ocr = patch('verifications.services.document_processor.extract_text_with_google_vision', return_value='PHILIPPINE NATIONAL ID MARIA SANTOS').start()
        self.ai = patch('verifications.services.document_processor.extract_structured_data_from_text', return_value={
            'first_name': 'Maria', 'last_name': 'Santos', 'philsys_number': '1234-5678',
            'date_issued': '2024-01-01',
        }).start()
        self.addCleanup(patch.stopall)

    def test_one_pipeline_saves_analysis_and_document_number_then_releases_ownership(self):
        self.assertTrue(processor.process_document(self.document.pk))
        self.document.refresh_from_db()
        self.assertEqual(self.document.document_number, '1234-5678')
        self.assertEqual(self.document.date_issued, date(2024, 1, 1))
        self.assertEqual(self.document.ocr_match_score, 100)
        self.assertIsNotNone(self.document.ocr_processed_at)
        self.assertIsNone(self.document.ocr_processing_token)
        self.assertEqual(self.document.verification_status, 'Pending')
        self.assertIsNone(processor.process_document(self.document.pk))
        self.fetch.assert_called_once()
        self.ai.assert_called_once()

    def test_analysis_cannot_undo_an_approval_or_rejection_made_during_processing(self):
        result = self.ai.return_value
        for status in ['Verified', 'Rejected']:
            tbl_documents.objects.filter(pk=self.document.pk).update(
                verification_status='Pending', ocr_last_attempt_at=None,
            )
            def review_during_analysis(*args):
                tbl_documents.objects.filter(pk=self.document.pk).update(
                    verification_status=status, verifyBy=self.reviewer, rejection_reason='Reviewer note',
                )
                return result
            self.ai.side_effect = review_during_analysis
            self.assertTrue(processor.process_document(self.document.pk, force=True))
            self.document.refresh_from_db()
            self.assertEqual(self.document.verification_status, status)
            self.assertEqual(self.document.verifyBy_id, self.reviewer.pk)
            self.assertEqual(self.document.rejection_reason, 'Reviewer note')

    def test_old_analysis_cannot_overwrite_a_replacement_or_release_its_new_attempt(self):
        new_token = uuid.uuid4()
        result = self.ai.return_value
        def replace_during_analysis(*args):
            tbl_documents.objects.filter(pk=self.document.pk).update(
                document_url='replacement-image', extracted_data={'new_upload': True},
                ocr_processing_token=new_token,
            )
            return result
        self.ai.side_effect = replace_during_analysis
        self.assertFalse(processor.process_document(self.document.pk))
        self.document.refresh_from_db()
        self.assertEqual(self.document.extracted_data, {'new_upload': True})
        self.assertIsNone(self.document.ocr_raw_text)
        self.assertIsNone(self.document.ocr_processed_at)
        self.assertEqual(self.document.ocr_processing_token, new_token)

    def test_deleted_document_is_not_recreated_by_a_finishing_worker(self):
        result = self.ai.return_value
        def delete_during_analysis(*args):
            self.document.delete()
            return result
        self.ai.side_effect = delete_during_analysis
        document_id = self.document.pk
        self.assertFalse(processor.process_document(document_id))
        self.assertFalse(tbl_documents.objects.filter(pk=document_id).exists())

    def test_repeated_status_reads_and_manual_reprocess_do_not_duplicate_a_live_job(self):
        with patch('verifications.services.document_processor.Thread') as thread:
            first = processor.process_document_async(self.document.pk)
            self.assertIsNotNone(first)
            # Even a document older than the old 30-second retry threshold is busy.
            tbl_documents.objects.filter(pk=self.document.pk).update(
                created_at=timezone.now() - timedelta(days=1),
                ocr_last_attempt_at=timezone.now() - timedelta(minutes=1),
            )
            for _ in range(4):
                self.assertEqual(self.client.get('/api/v1/verifications/status/').status_code, 200)
            self.assertIsNone(processor.process_document_async(self.document.pk, force=True))
            self.assertEqual(thread.return_value.start.call_count, 1)
        self.document.refresh_from_db()
        self.assertEqual(self.document.ocr_retry_count, 0)

    def test_expired_attempt_is_recovered_once_and_old_owner_cannot_finish(self):
        with patch('verifications.services.document_processor.Thread') as thread:
            processor.process_document_async(self.document.pk)
            old_document = thread.call_args.kwargs['args'][0]
            tbl_documents.objects.filter(pk=self.document.pk).update(
                ocr_last_attempt_at=timezone.now() - timedelta(minutes=6),
            )
            self.client.get('/api/v1/verifications/status/')
            self.client.get('/api/v1/verifications/status/')
            self.assertEqual(thread.return_value.start.call_count, 2)
        self.document.refresh_from_db()
        token = self.document.ocr_processing_token
        self.assertNotEqual(old_document.ocr_processing_token, token)
        self.assertEqual(self.document.ocr_retry_count, 1)
        self.assertFalse(processor._execute_claimed(old_document))
        self.fetch.assert_not_called()
        self.document.refresh_from_db()
        self.assertEqual(self.document.ocr_processing_token, token)

    def test_failure_retry_has_a_cooldown_and_a_three_retry_limit(self):
        self.fetch.side_effect = RuntimeError('provider unavailable')
        self.assertFalse(processor.process_document(self.document.pk))
        with patch('verifications.services.document_processor.Thread') as thread:
            self.client.get('/api/v1/verifications/status/')
            thread.assert_not_called()
            for expected in [1, 2, 3]:
                tbl_documents.objects.filter(pk=self.document.pk).update(
                    ocr_last_attempt_at=timezone.now() - timedelta(seconds=31),
                )
                self.assertFalse(processor.process_document(self.document.pk, retry=True))
                self.document.refresh_from_db()
                self.assertEqual(self.document.ocr_retry_count, expected)
                self.assertIsNone(self.document.ocr_processing_token)
            self.assertIsNone(processor.process_document(self.document.pk, retry=True))
            self.client.get('/api/v1/verifications/status/')
            thread.assert_not_called()

    def test_manual_reprocess_of_a_reviewed_document_preserves_its_status(self):
        tbl_documents.objects.filter(pk=self.document.pk).update(verification_status='Verified')
        self.assertIsNone(processor.process_document(self.document.pk))
        self.assertTrue(processor.process_document(self.document.pk, force=True))
        self.document.refresh_from_db()
        self.assertEqual(self.document.verification_status, 'Verified')

    def test_maintenance_command_uses_the_same_inflight_and_retry_guards(self):
        tbl_documents.objects.filter(pk=self.document.pk).update(created_at=timezone.now() - timedelta(minutes=10))
        with patch('verifications.services.document_processor.Thread') as thread:
            call_command('retry_failed_ocr', use_async=True, stdout=StringIO())
            call_command('retry_failed_ocr', use_async=True, stdout=StringIO())
            self.assertEqual(thread.return_value.start.call_count, 1)
        self.document.refresh_from_db()
        self.assertEqual(self.document.ocr_retry_count, 1)


class DocumentProcessingTransactionTests(AuditTransactionTestCase):
    def setUp(self):
        self.user = make_user()

    @patch('cloudinary.uploader.upload', return_value={'public_id': 'committed-image'})
    @patch('verifications.services.document_processor.process_document_async')
    def test_worker_is_started_after_commit_and_never_after_rollback(self, worker, cloud):
        with transaction.atomic():
            document = create_uploaded_document(self.user, image_file(), {'document_type': 'national_id_front'})
            worker.assert_not_called()
        worker.assert_called_once_with(str(document.pk), expected_url='committed-image')
        worker.reset_mock()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                create_uploaded_document(self.user, image_file(), {'document_type': 'national_id_back'})
                raise RuntimeError('roll back this upload')
        worker.assert_not_called()
        self.assertFalse(tbl_documents.objects.filter(document_type='national_id_back').exists())

    @skipUnlessDBFeature('has_select_for_update')
    def test_two_postgresql_connections_cannot_claim_the_same_document(self):
        document = tbl_documents.objects.create(
            user_profile=self.user, document_type='national_id_front', document_url='shared-image',
        )
        barrier = Barrier(2)
        def claim():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                result = processor._claim_document(document.pk)
                return result.ocr_processing_token if result is not None else None
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(claim) for _ in range(2)]
            tokens = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(token is not None for token in tokens), 1)
        document.refresh_from_db()
        self.assertIn(document.ocr_processing_token, tokens)
