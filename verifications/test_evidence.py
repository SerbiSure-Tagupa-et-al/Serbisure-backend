from datetime import date
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase
from .models import tbl_documents
from .serializers import AdminVerificationQueueSerializer
from .services.matching_service import detect_discrepancies, normalize_document_names
from .ocr_service import parse_extracted_document

User = get_user_model()


class VerificationEvidenceTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email='evidence-owner@test.com', username='evidence-owner',
                                             account_type='Homeowner', first_name='Maria', last_name='Santos')
        self.admin = User.objects.create_user(email='evidence-admin@test.com', username='evidence-admin', account_type='Admin')
        self.front = tbl_documents.objects.create(user_profile=self.owner, document_type='national_id_front', document_url='front', verification_status='Pending')
        self.back = tbl_documents.objects.create(user_profile=self.owner, document_type='national_id_back', document_url='back', verification_status='Pending')

    def test_approval_changes_only_selected_document(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post('/api/v1/verifications/admin/review/' + str(self.front.pk) + '/', {'action': 'approve'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.front.refresh_from_db()
        self.back.refresh_from_db()
        self.assertEqual(self.front.verification_status, 'Verified')
        self.assertEqual(self.back.verification_status, 'Pending')
        self.assertNotEqual(User.objects.get(pk=self.owner.pk).verification_status, 'Verified')
        self.assertEqual(self.front.verifyBy_id, self.admin.pk)
        from .views import AdminVerificationQueueView
        from rest_framework.test import APIRequestFactory, force_authenticate
        request = APIRequestFactory().get('/api/v1/verifications/admin/queue/?status=PENDING')
        force_authenticate(request, self.admin)
        response = AdminVerificationQueueView.as_view()(request)
        rows = response.data.get('results', []) if isinstance(response.data, dict) else response.data
        self.assertIn(str(self.back.pk), [row['id'] for row in rows])
        self.assertNotIn(str(self.front.pk), [row['id'] for row in rows])

    def test_queue_exposes_stable_applicant_identity_and_exact_submission_time(self):
        front = AdminVerificationQueueSerializer(self.front).data
        back = AdminVerificationQueueSerializer(self.back).data
        self.assertEqual(front['userId'], str(self.owner.pk))
        self.assertEqual(back['userId'], front['userId'])
        self.assertNotEqual(front['id'], back['id'])
        self.assertIn('T', front['submittedAt'])
        self.assertEqual(front['accountRegistration'], {
            'first_name': 'Maria', 'middle_name': None, 'last_name': 'Santos',
            'date_of_birth': None, 'address': None, 'barangay': None, 'city': None,
        })

    def test_both_staff_roles_see_printed_names_and_quality_flags_for_existing_ocr(self):
        self.owner.first_name = 'Christine'
        self.owner.middle_name = 'Cruz'
        self.owner.last_name = 'Santos'
        self.owner.date_of_birth = date(2002, 1, 1)
        self.owner.street = 'Lot 3'
        self.owner.barangay = 'Agusan'
        self.owner.city = 'Cagayan de Oro'
        self.owner.province = 'Misamis Oriental'
        self.owner.zipcode = '9000'
        self.owner.save()
        barangay = User.objects.create_user(
            email='evidence-barangay@test.com', username='evidence-barangay',
            account_type='Barangay', barangay='Agusan',
        )
        self.front.ocr_processed_at = timezone.now()
        self.front.ocr_raw_text = 'Apelyido/Last Name\nTAGUPA\nGiven Names / Mga Pangalan\nJAMES CHRISTOPHER'
        self.front.extracted_data = {'first_name': 'JAMES', 'last_name': 'CHRISTOPHER'}
        self.front.ocr_discrepancies = [
            {'field': 'last_name', 'document_value': 'CHRISTOPHER', 'severity': 'high'},
            {'field': 'date_of_birth', 'severity': 'high'},
        ]
        self.front.save()
        self.back.ocr_processed_at = timezone.now()
        self.back.ocr_raw_text = ''
        self.back.save()

        for actor in (self.admin, barangay):
            with self.subTest(role=actor.account_type):
                self.client.force_authenticate(actor)
                response = self.client.get('/api/v1/verifications/admin/queue/')
                self.assertEqual(response.status_code, 200, response.data)
                rows = {row['id']: row for row in response.data}
                front, back = rows[str(self.front.pk)], rows[str(self.back.pk)]
                self.assertEqual(front['accountName'], 'Christine Cruz Santos')
                self.assertEqual(front['accountRegistration'], {
                    'first_name': 'Christine', 'middle_name': 'Cruz', 'last_name': 'Santos',
                    'date_of_birth': '2002-01-01',
                    'address': 'Lot 3, Agusan, Cagayan de Oro, Misamis Oriental, 9000',
                    'barangay': 'Agusan', 'city': 'Cagayan de Oro',
                })
                self.assertEqual(back['accountRegistration'], front['accountRegistration'])
                self.assertEqual(front['ocrExtractedData']['full_name'], 'JAMES CHRISTOPHER TAGUPA')
                flags = {flag['field']: flag for flag in front['ocrDiscrepancies']}
                self.assertEqual(flags['first_name']['document_value'], 'JAMES CHRISTOPHER')
                self.assertEqual(flags['last_name']['document_value'], 'TAGUPA')
                self.assertIn('date_of_birth', flags)
                self.assertEqual(back['ocrDiscrepancies'][0]['field'], 'image_quality')
                self.assertIn('No readable text detected by OCR', back['ocrDiscrepancies'][0]['message'])
                self.assertEqual(back['recordStatus'], 'Flagged')

    def test_full_name_with_uncertain_parts_uses_document_evidence_without_guessing(self):
        data = {'full_name': 'MARIA TERESA SANTOS', 'first_name': 'MARIA', 'last_name': 'TERESA'}
        normalized = normalize_document_names(data, 'MARIA TERESA SANTOS')
        self.assertEqual(normalized['full_name'], data['full_name'])
        self.assertIsNone(normalized['first_name'])
        self.assertIsNone(normalized['last_name'])
        _, flags = detect_discrepancies(normalized, self.owner)
        self.assertEqual(flags, [])

    def test_unreadable_and_irrelevant_ocr_is_flagged_without_a_match_score(self):
        for text, message in (('', 'No readable text'), ('Screenshot: Settings', 'No readable ID details')):
            score, flags = detect_discrepancies({}, self.owner, document_type='national_id_front', raw_text=text)
            self.assertIsNone(score)
            self.assertEqual(flags[0]['field'], 'image_quality')
            self.assertIn(message, flags[0]['message'])
        for doc_type, evidence in (('national_id_back', {'blood_type': 'O+'}), ('profile_photo', {})):
            self.assertEqual(detect_discrepancies(evidence, self.owner, document_type=doc_type, raw_text=''), (None, []))
        self.assertEqual(AdminVerificationQueueSerializer(self.back).data['ocrDiscrepancies'], [])
        self.back.ocr_raw_text = 'Date issued: Nov 10, 2025'
        self.back.extracted_data = {'date_issued': '2025-11-10'}
        self.assertEqual(AdminVerificationQueueSerializer(self.back).data['ocrDiscrepancies'], [])

    def test_each_document_retains_its_own_status_reviewer_and_audit_history(self):
        from .models import tbl_audit_logs
        self.client.force_authenticate(self.admin)
        worker = User.objects.create_user(email='evidence-worker@test.com', username='evidence-worker', account_type='Kasambahay')
        nbi = tbl_documents.objects.create(user_profile=worker, document_type='nbi_clearance', document_url='nbi', verification_status='Pending')
        police = tbl_documents.objects.create(user_profile=worker, document_type='police_clearance', document_url='police', verification_status='Pending')
        tbl_documents.objects.create(user_profile=worker, document_type='tesda_nc2', document_url='tesda', verification_status='Verified')
        for first, second, applicant in ((self.front, self.back, self.owner), (nbi, police, worker)):
            tbl_documents.objects.create(user_profile=applicant, document_type='profile_photo',
                document_url='test/approved-photo', verification_status='Verified')
            def review(document, action):
                response = self.client.post(f'/api/v1/verifications/admin/review/{document.pk}/',
                    {'action': action, 'rejection_reason': 'Unreadable image'}, format='json')
                self.assertEqual(response.status_code, 200, response.data)
            review(first, 'approve')
            second.refresh_from_db()
            self.assertEqual(second.verification_status, 'Pending')
            self.assertIsNone(second.verifyBy_id)
            review(second, 'reject')
            first.refresh_from_db(); second.refresh_from_db()
            self.assertEqual(first.verification_status, 'Verified')
            self.assertEqual(second.verification_status, 'Rejected')
            self.assertEqual(User.objects.get(pk=applicant.pk).verification_status, 'Rejected')
            review(second, 'reset')
            first.refresh_from_db(); second.refresh_from_db()
            self.assertEqual(first.verification_status, 'Verified')
            self.assertEqual(second.verification_status, 'Pending')
            self.assertEqual(first.verifyBy_id, self.admin.pk)
            self.assertEqual(list(tbl_audit_logs.objects.filter(document=first, action__in=['APPROVED', 'REJECTED', 'RESET']).values_list('action', flat=True)), ['APPROVED'])
            self.assertCountEqual(tbl_audit_logs.objects.filter(document=second, action__in=['APPROVED', 'REJECTED', 'RESET']).values_list('action', flat=True), ['REJECTED', 'RESET'])
            review(second, 'approve')
            self.assertEqual(User.objects.get(pk=applicant.pk).verification_status, 'Verified')


    def test_anonymous_and_marketplace_users_cannot_approve(self):
        url = '/api/v1/verifications/admin/review/' + str(self.front.pk) + '/'
        self.assertIn(self.client.post(url, {'action': 'approve', 'reviewer_email': self.admin.email}).status_code, (401, 403))
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(url, {'action': 'approve'}).status_code, 403)
        self.front.refresh_from_db()
        self.assertEqual(self.front.verification_status, 'Pending')

    def test_baranggay_reviewer_cannot_cross_jurisdiction(self):
        reviewer = User.objects.create_user(email='evidence-official@test.com', username='evidence-official',
                                            account_type='Barangay', barangay='Pagatpat')
        self.owner.barangay = 'Canitoan'
        self.owner.save(update_fields=['barangay'])
        self.client.force_authenticate(reviewer)
        res = self.client.post('/api/v1/verifications/admin/review/' + str(self.front.pk) + '/', {'action': 'approve'})
        self.assertEqual(res.status_code, 403)

    def test_ocr_and_missing_measurement_never_become_facial_score(self):
        self.front.ocr_match_score = 97.5
        data = AdminVerificationQueueSerializer(self.front).data
        self.assertEqual(data['ocrMatchScore'], 97.5)
        self.assertIsNone(data['faceLivenessMatchScore'])
        self.front.ocr_match_score = None
        self.assertIsNone(AdminVerificationQueueSerializer(self.front).data['faceLivenessMatchScore'])

    def test_all_matching_uses_percentage_and_missing_evidence_is_none(self):
        score, _ = detect_discrepancies({'first_name': 'Maria', 'last_name': 'Santos'}, self.owner)
        self.assertEqual(score, 100.0)
        score, _ = detect_discrepancies({'document_number': '12345'}, self.owner)
        self.assertIsNone(score)
        with patch('verifications.ocr_service._call_groq_ai_verification', return_value={'first_name': 'Other', 'last_name': 'Person', 'match_score': 99.8}):
            result = parse_extracted_document('Document evidence', 'national_id_front', self.owner)
        expected, _ = detect_discrepancies({'first_name': 'Other', 'last_name': 'Person'}, self.owner)
        self.assertEqual(result['match_score'], expected)
        self.assertNotEqual(result['match_score'], 99.8)
        self.assertIsNone(result['document_number'])
