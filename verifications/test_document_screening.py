"""Automated 5-case verification screening test proof (Chapters 4 & 5).

Proves Option B behavior across all five panelist scenarios:
  (a) Valid, unexpired NBI Clearance -> all 4 checks pass, high match score, 1-click approval
  (b) Valid, expired NBI Clearance -> Pending with expired flag, approval locked unless overridden, re-upload unlocked
  (c) Unrelated image carrying the user's name -> Document Type Mismatch, 0% match score, Pending, approval locked
  (d) Fabricated / sample NBI document -> Suspicious / Incomplete flagged, 0% match score, Pending, approval locked
  (e) Blurry / unreadable photo -> Image Clarity failed, Pending, approval locked, re-upload unlocked
Plus cross-slot swap detection, legacy Pending backward compatibility, and audited override approval.
"""

from datetime import date, timedelta
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase

from verifications.models import tbl_audit_logs, tbl_documents
from verifications.serializers import AdminVerificationQueueSerializer
from verifications.serializers_admin import UserDocumentStatusSerializer
from verifications.services import document_processor as processor

User = get_user_model()


def _fake_upload_file():
    buf = BytesIO()
    Image.new('RGB', (24, 24), 'white').save(buf, format='JPEG')
    return SimpleUploadedFile('replacement.jpg', buf.getvalue(), content_type='image/jpeg')


class DocumentScreeningFiveCaseProofTests(APITestCase):
    def setUp(self):
        self.worker = User.objects.create_user(
            username='screening-worker',
            email='screening-worker@test.com',
            account_type='Kasambahay',
            first_name='Maria',
            middle_name='Clara',
            last_name='Santos',
            date_of_birth=date(1996, 5, 14),
            barangay='Pagatpat',
        )
        self.admin = User.objects.create_user(
            username='screening-admin',
            email='screening-admin@test.com',
            account_type='Admin',
        )
        patch(
            'verifications.services.document_processor.fetch_image_from_cloudinary',
            return_value=(Image.new('RGB', (24, 24)), b'fake-bytes'),
        ).start()
        patch(
            'verifications.services.document_processor.is_google_vision_available',
            return_value=True,
        ).start()
        self.addCleanup(patch.stopall)

    def _run_pipeline(self, *, doc_type='nbi_clearance', raw_text='', ai_payload=None):
        doc = tbl_documents.objects.create(
            user_profile=self.worker,
            document_type=doc_type,
            document_url=f'test/{doc_type}-{tbl_documents.objects.count()}',
            verification_status='Pending',
        )
        with patch(
            'verifications.services.document_processor.extract_text_with_google_vision',
            return_value=raw_text,
        ), patch(
            'verifications.services.document_processor.extract_structured_data_from_text',
            return_value=dict(ai_payload or {}),
        ), patch(
            'verifications.services.document_processor.extract_from_image_vision',
            return_value=(raw_text, dict(ai_payload or {})),
        ):
            self.assertTrue(processor.process_document(doc.pk))
        doc.refresh_from_db()
        return doc

    def test_case_a_valid_current_nbi_clearance_passes_all_four_checks_and_approves_normally(self):
        future_expiry = (date.today() + timedelta(days=240)).isoformat()
        issued = (date.today() - timedelta(days=125)).isoformat()
        doc = self._run_pipeline(
            doc_type='nbi_clearance',
            raw_text=(
                'REPUBLIC OF THE PHILIPPINES\n'
                'DEPARTMENT OF JUSTICE\n'
                'NATIONAL BUREAU OF INVESTIGATION\n'
                'MULTI-PURPOSE CLEARANCE\n'
                'NBI ID No: M3912ABC9081\n'
                'Name: SANTOS, MARIA CLARA\n'
                'Date of Birth: 1996-05-14\n'
                f'Date Issued: {issued}\n'
                f'Valid Until: {future_expiry}\n'
                'NO RECORD ON FILE'
            ),
            ai_payload={
                'full_name': 'MARIA CLARA SANTOS',
                'first_name': 'MARIA',
                'middle_name': 'CLARA',
                'last_name': 'SANTOS',
                'date_of_birth': '1996-05-14',
                'clearance_number': 'M3912ABC9081',
                'date_issued': issued,
                'valid_until': future_expiry,
                'detected_document_type': 'nbi_clearance',
                'suspicion_reason': None,
            },
        )
        self.assertEqual(doc.verification_status, 'Pending')
        self.assertEqual(doc.ocr_match_score, 100.0)

        admin_data = AdminVerificationQueueSerializer(doc).data
        screening = admin_data['screening']
        self.assertEqual(screening['checks'], {
            'document_type_match': 'pass',
            'name_match': 'pass',
            'not_expired': 'pass',
            'image_clarity': 'pass',
        })
        self.assertFalse(screening['has_blocking_flag'])
        self.assertIsNone(screening['user_alert'])

        # Clean Pending doc remains locked from replacement while awaiting review
        user_data = UserDocumentStatusSerializer(doc).data
        self.assertFalse(user_data['can_replace'])

        # Normal 1-click approval succeeds without override
        self.client.force_authenticate(self.admin)
        res = self.client.post(f'/api/v1/verifications/admin/review/{doc.pk}/', {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        doc.refresh_from_db()
        self.assertEqual(doc.verification_status, 'Verified')

    def test_case_b_valid_expired_nbi_clearance_stays_pending_flags_expiry_locks_approval_and_unlocks_reupload(self):
        past_expiry = (date.today() - timedelta(days=45)).isoformat()
        issued = (date.today() - timedelta(days=410)).isoformat()
        doc = self._run_pipeline(
            doc_type='nbi_clearance',
            raw_text=(
                'DEPARTMENT OF JUSTICE\n'
                'NATIONAL BUREAU OF INVESTIGATION\n'
                'NBI CLEARANCE\n'
                'NBI ID No: M9911XYZ7711\n'
                'Name: MARIA CLARA SANTOS\n'
                f'Date Issued: {issued}\n'
                f'Valid Until: {past_expiry}'
            ),
            ai_payload={
                'full_name': 'MARIA CLARA SANTOS',
                'first_name': 'MARIA',
                'middle_name': 'CLARA',
                'last_name': 'SANTOS',
                'date_of_birth': '1996-05-14',
                'clearance_number': 'M9911XYZ7711',
                'date_issued': issued,
                'valid_until': past_expiry,
                'detected_document_type': 'nbi_clearance',
                'suspicion_reason': None,
            },
        )
        # Option B: stays Pending
        self.assertEqual(doc.verification_status, 'Pending')

        user_data = UserDocumentStatusSerializer(doc).data
        screening = user_data['screening']
        self.assertEqual(screening['checks']['document_type_match'], 'pass')
        self.assertEqual(screening['checks']['not_expired'], 'fail')
        self.assertEqual(screening['primary_issue'], 'expired')
        self.assertTrue(screening['has_blocking_flag'])
        self.assertEqual(screening['user_alert']['level'], 'warning')
        self.assertIn('expired', screening['user_alert']['message'].lower())
        self.assertTrue(user_data['can_replace'])

        # Admin 1-click approve is blocked without override
        self.client.force_authenticate(self.admin)
        blocked = self.client.post(f'/api/v1/verifications/admin/review/{doc.pk}/', {'action': 'approve'}, format='json')
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked.data.get('code'), 'override_required')

    def test_case_c_unrelated_image_with_users_name_drops_score_to_zero_and_blocks_one_click_approval(self):
        doc = self._run_pipeline(
            doc_type='nbi_clearance',
            raw_text=(
                'SM SUPERMARKET CAGAYAN DE ORO\n'
                'OFFICIAL RECEIPT #0044123\n'
                'Customer: MARIA CLARA SANTOS\n'
                'Subtotal: PHP 450.00\n'
                'Grand Total: PHP 450.00'
            ),
            ai_payload={
                'full_name': 'MARIA CLARA SANTOS',
                'first_name': 'MARIA',
                'middle_name': 'CLARA',
                'last_name': 'SANTOS',
                'detected_document_type': 'unrelated',
                'suspicion_reason': 'Image appears to be a supermarket receipt, not an NBI Clearance.',
            },
        )
        self.assertEqual(doc.verification_status, 'Pending')
        # Critical fix: unrelated image with user's name MUST score 0%, never 100%!
        self.assertEqual(doc.ocr_match_score, 0.0)

        admin_data = AdminVerificationQueueSerializer(doc).data
        screening = admin_data['screening']
        self.assertEqual(screening['checks']['document_type_match'], 'fail')
        self.assertEqual(screening['primary_issue'], 'wrong_document')
        self.assertTrue(screening['has_blocking_flag'])
        self.assertEqual(admin_data['recordStatus'], 'Flagged')
        self.assertEqual(screening['user_alert']['level'], 'error')
        self.assertIn('Wrong document detected', screening['user_alert']['message'])

        # User can immediately re-upload a replacement even while Pending (Option B)
        self.client.force_authenticate(self.worker)
        with patch('cloudinary.uploader.upload', return_value={'public_id': 'replaced-nbi'}), \
             patch('verifications.services.document_processor.Thread'):
            reupload = self.client.post(
                '/api/v1/verifications/upload/',
                {'document_type': 'nbi_clearance', 'document_image': _fake_upload_file()},
                format='multipart',
            )
        self.assertEqual(reupload.status_code, 201, reupload.data)

    def test_case_d_fabricated_or_sample_nbi_document_is_flagged_and_locked_in_admin(self):
        # Subcase D1: Sample/dummy template wording
        sample_doc = self._run_pipeline(
            doc_type='nbi_clearance',
            raw_text=(
                'DEPARTMENT OF JUSTICE\n'
                'NATIONAL BUREAU OF INVESTIGATION\n'
                'NBI CLEARANCE - SAMPLE ONLY\n'
                'Name: MARIA CLARA SANTOS\n'
                'NBI ID No: M0000SAMPLE01'
            ),
            ai_payload={
                'full_name': 'MARIA CLARA SANTOS',
                'first_name': 'MARIA',
                'last_name': 'SANTOS',
                'clearance_number': 'M0000SAMPLE01',
                'detected_document_type': 'nbi_clearance',
                'suspicion_reason': 'Contains SAMPLE watermark',
            },
        )
        self.assertEqual(sample_doc.verification_status, 'Pending')
        self.assertEqual(sample_doc.ocr_match_score, 0.0)
        sample_screening = AdminVerificationQueueSerializer(sample_doc).data['screening']
        self.assertEqual(sample_screening['checks']['document_type_match'], 'fail')
        self.assertEqual(sample_screening['primary_issue'], 'suspicious')
        self.assertTrue(sample_screening['has_blocking_flag'])

        # Subcase D2: Typed stub claiming "NBI CLEARANCE" with no clearance number or issue/expiry dates
        stub_doc = self._run_pipeline(
            doc_type='nbi_clearance',
            raw_text='NBI CLEARANCE\nName: MARIA CLARA SANTOS\nAddress: Pagatpat, Cagayan de Oro',
            ai_payload={
                'full_name': 'MARIA CLARA SANTOS',
                'first_name': 'MARIA',
                'last_name': 'SANTOS',
                'detected_document_type': 'nbi_clearance',
                'suspicion_reason': None,
            },
        )
        self.assertEqual(stub_doc.verification_status, 'Pending')
        self.assertEqual(stub_doc.ocr_match_score, 0.0)
        stub_screening = AdminVerificationQueueSerializer(stub_doc).data['screening']
        self.assertEqual(stub_screening['checks']['document_type_match'], 'fail')
        self.assertEqual(stub_screening['primary_issue'], 'suspicious')
        self.assertTrue(stub_screening['has_blocking_flag'])

        # Admin cannot 1-click approve without override; override + justification succeeds and writes audit log
        self.client.force_authenticate(self.admin)
        blocked = self.client.post(f'/api/v1/verifications/admin/review/{stub_doc.pk}/', {'action': 'approve'}, format='json')
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(blocked.data.get('code'), 'override_required')

        no_reason = self.client.post(
            f'/api/v1/verifications/admin/review/{stub_doc.pk}/',
            {'action': 'approve', 'override_warning': True, 'override_reason': '   '},
            format='json',
        )
        self.assertEqual(no_reason.status_code, 400)

        overridden = self.client.post(
            f'/api/v1/verifications/admin/review/{stub_doc.pk}/',
            {
                'action': 'approve',
                'override_warning': True,
                'override_reason': 'Verified physical original clearance at Barangay Hall desk.',
            },
            format='json',
        )
        self.assertEqual(overridden.status_code, 200, overridden.data)
        stub_doc.refresh_from_db()
        self.assertEqual(stub_doc.verification_status, 'Verified')
        log = tbl_audit_logs.objects.filter(document=stub_doc, action='APPROVED').first()
        self.assertIsNotNone(log)
        self.assertIn('Verified physical original clearance', log.reason or '')
        self.assertTrue((log.metadata or {}).get('override_warning'))

    def test_case_e_blurry_unreadable_photo_fails_image_clarity_and_prompts_retake(self):
        doc = self._run_pipeline(
            doc_type='nbi_clearance',
            raw_text='...  ',
            ai_payload={'detected_document_type': 'unreadable', 'suspicion_reason': 'Image is too blurry to read.'},
        )
        self.assertEqual(doc.verification_status, 'Pending')
        self.assertIsNone(doc.ocr_match_score)

        user_data = UserDocumentStatusSerializer(doc).data
        screening = user_data['screening']
        self.assertEqual(screening['checks']['image_clarity'], 'fail')
        self.assertEqual(screening['primary_issue'], 'unreadable')
        self.assertTrue(screening['has_blocking_flag'])
        self.assertEqual(screening['user_alert']['code'], 'unreadable')
        self.assertIn('too blurry or unreadable', screening['user_alert']['message'].lower())
        self.assertTrue(user_data['can_replace'])

    def test_cross_slot_swap_and_legacy_pending_document_backward_compatibility(self):
        # Police Clearance uploaded into NBI Clearance slot on a legacy Pending row (no _screening saved)
        legacy_doc = tbl_documents.objects.create(
            user_profile=self.worker,
            document_type='nbi_clearance',
            document_url='legacy/police-in-nbi',
            verification_status='Pending',
            ocr_processed_at=timezone.now(),
            ocr_raw_text=(
                'PHILIPPINE NATIONAL POLICE\n'
                'NATIONAL POLICE CLEARANCE\n'
                'Name: MARIA CLARA SANTOS\n'
                'Clearance No: PNP-2026-009812'
            ),
            extracted_data={'first_name': 'MARIA', 'last_name': 'SANTOS', 'clearance_number': 'PNP-2026-009812'},
            ocr_match_score=100.0,
            ocr_discrepancies=[],
        )
        admin_data = AdminVerificationQueueSerializer(legacy_doc).data
        screening = admin_data['screening']
        self.assertEqual(screening['checks']['document_type_match'], 'fail')
        self.assertEqual(screening['detected_document_type'], 'police_clearance')
        self.assertEqual(admin_data['ocrMatchScore'], 0.0)
        self.assertEqual(admin_data['recordStatus'], 'Flagged')
        self.assertIn('Police Clearance', screening['user_alert']['message'])
