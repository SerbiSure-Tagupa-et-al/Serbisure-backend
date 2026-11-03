from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APITestCase

from accounts.models import tbl_user_profile as User
from accounts.workforce import credential_snapshot
from booking.services import ensure_eligible_pair
from verifications.models import tbl_documents as Document, tbl_audit_logs as Audit
from verifications.services.document_processor import document_needs_retry, process_document_async
from verifications.test_document_processing import image_file


class ProfilePhotoTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner = User.objects.create_user(email='photo-owner@example.test', username='photo-owner', account_type='Homeowner', barangay='Pagatpat')
        self.worker = User.objects.create_user(email='photo-worker@example.test', username='photo-worker', account_type='Kasambahay', barangay='Pagatpat')
        self.admin = User.objects.create_user(email='photo-admin@example.test', username='photo-admin', account_type='Admin')
        self.desk = User.objects.create_user(email='photo-desk@example.test', username='photo-desk', account_type='Barangay', barangay='Brgy. Pagatpat')
        self.other = User.objects.create_user(email='photo-other@example.test', username='photo-other', account_type='Barangay', barangay='Agusan')
        self.client.force_authenticate(self.owner)

    def photo(self, user=None, status='Pending'):
        return Document.objects.create(user_profile=user or self.owner, document_type='profile_photo',
            document_url='test/profile-photo', verification_status=status)

    def credentials(self, user):
        kinds = ('national_id_front', 'national_id_back') if user.account_type == 'Homeowner' else ('nbi_clearance', 'police_clearance')
        for kind in kinds:
            Document.objects.create(user_profile=user, document_type=kind, document_url=f'test/{kind}', verification_status='Verified')

    @patch('accounts.profile_images.cloudinary.uploader.upload', return_value={'public_id': 'test/registration-selfie'})
    @patch('verifications.services.document_processor.process_document_async')
    def test_registration_selfie_enters_review_and_syncs_avatar_without_ocr(self, processor, upload):
        response = self.client.put('/api/v1/accounts/profile-image/', {'profile_image': image_file()}, format='multipart')
        self.assertEqual(response.status_code, 200, response.data)
        document = self.owner.documents.get(document_type='profile_photo')
        self.assertEqual(document.verification_status, 'Pending')
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.profile_link, document.document_url)
        self.assertEqual(Audit.objects.filter(document=document, action='UPLOADED').count(), 1)
        self.assertFalse(document_needs_retry(document))
        self.client.get('/api/v1/verifications/status/')
        processor.assert_not_called()

    def test_documents_alone_do_not_verify_either_role_and_rejected_photo_blocks_bookings(self):
        for user in (self.owner, self.worker):
            self.credentials(user)
            self.assertNotEqual(user.verification_status, 'Verified')
            self.photo(user, 'Verified')
            self.assertEqual(user.verification_status, 'Verified')
        photo = self.worker.documents.get(document_type='profile_photo')
        photo.verification_status = 'Rejected'; photo.save()
        self.assertEqual(credential_snapshot(self.worker)[0], 'rejected')
        with self.assertRaises(PermissionDenied): ensure_eligible_pair(self.owner, self.worker)
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.client.post('/api/v1/booking/post/', {}, format='json').status_code, 403)
        self.assertTrue(self.worker.documents.exclude(document_type='profile_photo').filter(verification_status='Verified').count() == 2)

    def test_review_requires_reason_respects_jurisdiction_and_logs_and_notifies(self):
        photo = self.photo()
        url = f'/api/v1/verifications/admin/review/{photo.pk}/'
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(url, {'action': 'approve'}, format='json').status_code, 403)
        self.client.force_authenticate(self.desk)
        self.assertEqual(self.client.post(url, {'action': 'reject', 'rejection_reason': ' '}, format='json').status_code, 400)
        result = self.client.post(url, {'action': 'reject', 'rejection_reason': 'Please use a brighter place.'}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        log = Audit.objects.get(document=photo, action='REJECTED')
        self.assertEqual((log.actor_id, log.target_user_id, log.document_type, log.previous_status, log.new_status),
                         (self.desk.pk, self.owner.pk, 'profile_photo', 'Pending', 'Rejected'))
        self.assertEqual(log.reason, 'Please use a brighter place.')
        self.assertTrue(self.owner.received_notifications.filter(notification_message__icontains='brighter').exists())

    @patch('accounts.profile_images.cloudinary.uploader.upload', return_value={'public_id': 'test/retaken-photo'})
    def test_retake_only_rejected_photo_returns_pending_and_audits_once(self, upload):
        photo = self.photo(status='Rejected')
        photo.rejection_reason = 'Too dark'; photo.verifyBy = self.admin; photo.save()
        result = self.client.put('/api/v1/accounts/profile-image/', {'profile_image': image_file()}, format='multipart')
        self.assertEqual(result.status_code, 200, result.data)
        photo.refresh_from_db(); self.owner.refresh_from_db()
        self.assertEqual((photo.verification_status, photo.rejection_reason, photo.verifyBy_id), ('Pending', None, None))
        self.assertEqual(self.owner.profile_link, 'test/retaken-photo')
        self.assertEqual(self.owner.documents.filter(document_type='profile_photo').count(), 1)
        self.assertEqual(Audit.objects.filter(document=photo, action='UPLOADED', previous_status='Rejected', new_status='Pending').count(), 1)
        result = self.client.put('/api/v1/accounts/profile-image/', {'profile_image': image_file()}, format='multipart')
        self.assertEqual(result.status_code, 403)
        upload.assert_called_once()

    @patch('accounts.profile_images.cloudinary.uploader.upload')
    def test_verified_photo_is_locked_and_cannot_use_document_upload_or_delete_bypass(self, upload):
        photo = self.photo(status='Verified')
        self.assertEqual(self.client.put('/api/v1/accounts/profile-image/', {'profile_image': image_file()}, format='multipart').status_code, 403)
        self.assertEqual(self.client.post('/api/v1/verifications/upload/', {'document_type': 'profile_photo', 'document_image': image_file()}, format='multipart').status_code, 400)
        photo.verification_status = 'Rejected'; photo.save()
        self.assertEqual(self.client.delete(f'/api/v1/verifications/documents/{photo.pk}/').status_code, 403)
        upload.assert_not_called()

    def test_alternative_review_endpoint_has_same_scope_and_photo_never_runs_ocr(self):
        photo = self.photo()
        url = f'/api/v1/verifications/admin/documents/{photo.pk}/action/'
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.patch(url, {'verification_status': 'Verified'}, format='json').status_code, 403)
        self.client.force_authenticate(self.admin)
        result = self.client.patch(url, {'verification_status': 'Verified'}, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(Audit.objects.filter(document=photo, action='APPROVED').count(), 1)
        self.assertEqual(self.client.post(f'/api/v1/verifications/admin/documents/{photo.pk}/reprocess/').status_code, 400)
        self.assertIsNone(process_document_async(str(photo.pk), force=True))

    def test_grandfathering_preserves_existing_photos_and_reviews_and_is_idempotent(self):
        User.objects.filter(pk=self.owner.pk).update(profile_link='existing/photo')
        rejected = self.photo(self.worker, 'Rejected')
        state = MigrationExecutor(connection).loader.project_state([('verifications', '0013_grandfather_profile_photos')]).apps
        migration = import_module('verifications.migrations.0013_grandfather_profile_photos')
        editor = SimpleNamespace(connection=connection)
        migration.grandfather_photos(state, editor); migration.grandfather_photos(state, editor)
        photo = self.owner.documents.get(document_type='profile_photo')
        self.assertEqual((photo.document_url, photo.verification_status), ('existing/photo', 'Verified'))
        rejected.refresh_from_db(); self.assertEqual(rejected.verification_status, 'Rejected')
        self.assertEqual(Audit.objects.filter(document=photo, action='APPROVED').count(), 1)

    def test_queue_and_status_include_photo_as_required_and_avatar_follows_saved_document(self):
        photo = self.photo()
        self.client.force_authenticate(self.admin)
        rows = self.client.get('/api/v1/verifications/admin/queue/').data
        row = next(row for row in rows if row.get('rawDocumentType') == 'profile_photo')
        self.assertEqual(row['documentType'], 'Profile Photo')
        self.client.force_authenticate(self.owner)
        response = self.client.get('/api/v1/verifications/status/')
        self.assertIn('profile_photo', response.data['required_documents'])
        self.assertIn('profile_link', response.data)
        self.assertFalse(response.data['documents'][0]['can_replace'])
        self.assertEqual(photo.document_url, self.owner.profile_link)

    def test_alternate_photo_list_and_detail_do_not_leak_outside_barangay(self):
        photo = self.photo()
        self.client.force_authenticate(self.other)
        response = self.client.get('/api/v1/verifications/admin/documents/?document_type=profile_photo')
        rows = response.data.get('results', []) if isinstance(response.data, dict) else response.data
        self.assertEqual(rows, [])
        self.assertEqual(self.client.get(f'/api/v1/verifications/admin/documents/{photo.pk}/').status_code, 404)
        self.client.force_authenticate(self.desk)
        self.assertEqual(self.client.get(f'/api/v1/verifications/admin/documents/{photo.pk}/').status_code, 200)

    @patch('verifications.services.profile_photos.record_audit_log', return_value=None)
    def test_review_rolls_back_if_audit_cannot_be_saved(self, audit):
        photo = self.photo()
        self.client.force_authenticate(self.admin)
        response = self.client.post(f'/api/v1/verifications/admin/review/{photo.pk}/', {'action': 'approve'}, format='json')
        self.assertEqual(response.status_code, 500)
        photo.refresh_from_db()
        self.assertEqual(photo.verification_status, 'Pending')
