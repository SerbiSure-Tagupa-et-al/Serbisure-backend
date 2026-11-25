import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch
from uuid import uuid4

from django.core.cache import cache
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.models import tbl_user_profile as User
from accounts.testing import create_verified_test_user
from accounts.workforce import PH_TIME
from booking.models import tbl_booking as Booking
from booking.services import ensure_eligible_pair
from verifications.models import tbl_documents as Document
from verifications.serializers import AdminVerificationQueueSerializer
from verifications.services.document_fields import merge_document_fields
from verifications.services.expiry import expired_candidates
from verifications.services.groq_service import (
    DOCUMENT_SCHEMAS, NAME_INSTRUCTIONS, TESDA_NAME_AND_FIELD_INSTRUCTIONS,
    build_system_prompt, extract_from_image_vision, extract_structured_data_from_text,
)
from verifications.services.matching_service import detect_discrepancies
from verifications.test_document_processing import image_file


class TesdaExtractionTests(SimpleTestCase):
    legacy_types = ('nbi_clearance', 'police_clearance', 'national_id_front', 'national_id_back', 'profile_photo')

    def test_tesda_prompt_targets_holder_and_certificate_fields(self):
        prompt = build_system_prompt('tesda_nc2')
        for instruction in ('is awarded to', 'never the bottom signatory', 'Director General',
                            'MARIA CLARA S. DELA CRUZ', 'first_name MARIA CLARA', 'middle_name S',
                            'last_name DELA CRUZ', 'DE LA CRUZ', 'DE LOS SANTOS', 'DE LEON',
                            'SAN JOSE', 'STA. MARIA', 'Certificate No:', 'never ULI:', 'CLN-NQ-',
                            'HOUSEKEEPING', 'issuing_body', '2018-09-20', '2023-09-19', 'null if not printed'):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, prompt)
        self.assertNotIn('date_of_birth', DOCUMENT_SCHEMAS['tesda_nc2']['fields'])
        self.assertNotIn('address', DOCUMENT_SCHEMAS['tesda_nc2']['fields'])

    @patch('verifications.services.groq_service.get_groq_client')
    def test_text_and_vision_use_tesda_instructions_only_for_tesda(self, get_client):
        client = get_client.return_value = MagicMock()
        sample = dict(full_name='MARIA CLARA S. DELA CRUZ', first_name='MARIA CLARA', middle_name='S',
                      last_name='DELA CRUZ', certificate_number='21140302025983', qualification_title='HOUSEKEEPING',
                      issuing_body='TESDA', date_issued='2018-09-20', valid_until='2023-09-19')
        legacy_name = (
            'Preserve every word in a printed Given Name(s) or First Name field as first_name, including continuation lines. '
            'Use the printed Surname or Last Name field for last_name. '
            'For example, Given Names MARIA TERESA and Last Name REYES means '
            'first_name MARIA TERESA and last_name REYES. '
            'If only an unlabeled full name is visible and the parts are uncertain, return full_name and leave the parts null.'
        )
        self.assertEqual(NAME_INSTRUCTIONS, legacy_name)
        for kind in ('tesda_nc2', *self.legacy_types):
            with self.subTest(kind=kind):
                schema = DOCUMENT_SCHEMAS.get(kind, {})
                fields = ', '.join(schema.get('fields', ['full_name', 'date_issued', 'valid_until']))
                instructions = TESDA_NAME_AND_FIELD_INSTRUCTIONS if kind == 'tesda_nc2' else legacy_name
                client.chat.completions.create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(sample)))])
                self.assertEqual(extract_structured_data_from_text('certificate text', kind), sample)
                system = client.chat.completions.create.call_args.kwargs['messages'][0]['content']
                self.assertEqual(system, (
                    'You are a specialized document data extraction AI for the Philippines SerbiSure platform. '
                    f'You extract and structure data from {schema.get("description", kind)}.\n'
                    f'Extract the following keys:\n{fields}\n\nRules:\n'
                    '1. Return ONLY a single valid JSON object containing the specified keys.\n'
                    '2. All dates MUST be in YYYY-MM-DD format (convert from English words or MM/DD/YYYY if necessary). If unavailable or unreadable, return null.\n'
                    '3. If any field cannot be found or is blurry, set its value to null. Do not invent or guess.\n'
                    f'4. {instructions}'
                ))
                client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps({'raw_text': 'certificate text', 'extracted_data': sample})))])
                self.assertEqual(extract_from_image_vision(b'image', kind), ('certificate text', sample))
                vision = client.chat.completions.create.call_args.kwargs['messages'][0]['content'][0]['text']
                self.assertEqual(vision, (
                    f"You are verifying a Philippine document of type '{kind}'.\n"
                    'First, read all visible printed text on this document verbatim.\n'
                    f'Second, extract the structured fields: {fields}.\n\n{instructions}\n\n'
                    'Return a JSON object with exactly two keys:\n'
                    '1. "raw_text": A string with all readable text transcribed from the image.\n'
                    '2. "extracted_data": A JSON dictionary with the extracted fields (dates in YYYY-MM-DD, null if missing).'
                ))

    def test_tesda_middle_initials_match_only_the_first_letter_in_either_direction(self):
        for account, document, expected in (
            ('Santos', 'S', 100), ('Santos', 'S.', 100), ('s.', 'SANTOS', 100),
            ('S', 'S.', 100), ('Reyes', 'S', 0), ('S.', 'Reyes', 0),
            ('Santos', 'R.', 0), ('Santos', 'Santos', 100),
        ):
            with self.subTest(account=account, document=document):
                score, flags = detect_discrepancies({'middle_name': document},
                    SimpleNamespace(middle_name=account), document_type='tesda_nc2')
                self.assertEqual(score, expected)
                self.assertEqual(any(flag['field'] == 'middle_name' for flag in flags), expected == 0)

    def test_tesda_ignores_legacy_birthdate_but_other_types_keep_existing_matching(self):
        profile = SimpleNamespace(middle_name='Reyes', date_of_birth='2004-06-05')
        score, flags = detect_discrepancies({'middle_name': 'R', 'date_of_birth': '1990-01-01'},
                                            profile, document_type='tesda_nc2')
        self.assertEqual(score, 100)
        self.assertEqual(flags, [])
        for kind in self.legacy_types:
            with self.subTest(kind=kind):
                score, flags = detect_discrepancies({'middle_name': 'S'}, profile, document_type=kind)
                self.assertEqual(score, 85)
                self.assertEqual(flags, [])
                _, flags = detect_discrepancies({'date_of_birth': '1990-01-01'}, profile, document_type=kind)
                self.assertEqual([flag['field'] for flag in flags], ['date_of_birth'])


class SkillCertificationTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.worker = User.objects.create_user(email='legacy-worker@test.com', username='legacy-worker',
                                               account_type='Kasambahay', barangay='Pagatpat')
        self.old_docs = [Document.objects.create(user_profile=self.worker, document_type=kind,
                         document_url=f'test/{kind}', verification_status='Verified')
                         for kind in ('nbi_clearance', 'police_clearance', 'profile_photo')]
        self.owner = create_verified_test_user(email='certificate-owner@test.com', username='certificate-owner',
                                              account_type='Homeowner', verification_status='Verified')
        self.admin = User.objects.create_user(email='certificate-admin@test.com', username='certificate-admin', account_type='Admin')
        self.desk = User.objects.create_user(email='certificate-desk@test.com', username='certificate-desk',
                                            account_type='Barangay', barangay='Pagatpat')
        self.start = timezone.now() + timedelta(days=1)
        self.end = self.start + timedelta(days=1)
        self.booking = Booking.objects.create(poster_id=self.owner, booking_type='short_term',
            booking_status='Pending', service_category=['Cleaning'], region='Region X - Northern Mindanao',
            rate_amount='800', rate_period='day', start_time=self.start, end_time=self.end)
        self.client.force_authenticate(self.worker)

    def payload(self):
        return dict(booking_type='short_term', work_setup='stay_out', rate_period='day',
                    service_category=['Cleaning'], region='Region X - Northern Mindanao', province='Misamis Oriental',
                    city='Cagayan de Oro', barangay='Pagatpat', street='123 Test St', zip_code='9000',
                    rate_amount='800.00', start_time=self.start.isoformat(), end_time=self.end.isoformat())

    def test_old_approvals_are_preserved_but_missing_certificate_blocks_every_booking_entry(self):
        response = self.client.get(reverse('document-status'))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn('tesda_nc2', response.data['required_documents'])
        self.assertEqual(response.data['overall_status'], 'Pending')
        self.assertEqual(len(response.data['documents']), 3)
        self.assertTrue(all(doc['verification_status'] == 'Verified' for doc in response.data['documents']))
        response = self.client.post(reverse('booking-post'), self.payload(), format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(response.status_code, 403, response.data)
        response = self.client.post(reverse('booking-proposals-create', args=[self.booking.pk]),
            {'proposed_rate': '800', 'rate_period': 'day'}, format='json')
        self.assertEqual(response.status_code, 403, response.data)
        response = self.client.patch(reverse('booking-accept', args=[self.booking.pk]), {}, format='json')
        self.assertEqual(response.status_code, 403, response.data)
        self.booking.refresh_from_db()
        self.assertEqual(self.booking.booking_status, 'Pending')
        for actor in (self.admin, self.desk):
            self.client.force_authenticate(actor)
            response = self.client.get(reverse('admin-verification-queue'))
            self.assertEqual(response.status_code, 200)
            rows = [row for row in response.data if row['userId'] == str(self.worker.pk)]
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row['applicantStatus'] == 'PENDING / REVIEW' for row in rows))

    @patch('verifications.services.document_processor.process_document_async')
    @patch('verifications.services.document_upload.cloudinary.uploader.upload', return_value={'public_id': 'test/certificate'})
    def test_missing_certificate_upload_and_official_approval_restore_eligibility(self, upload, processor):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('document-upload'),
                {'document_type': 'tesda_nc2', 'document_image': image_file()}, format='multipart')
        self.assertEqual(response.status_code, 201, response.data)
        certificate = self.worker.documents.get(document_type='tesda_nc2')
        self.assertEqual(certificate.verification_status, 'Pending')
        processor.assert_called_once_with(str(certificate.pk), expected_url='test/certificate')
        self.assertNotEqual(self.worker.verification_status, 'Verified')
        for actor in (self.admin, self.desk):
            Document.objects.filter(pk=certificate.pk).update(verification_status='Pending')
            self.client.force_authenticate(actor)
            result = self.client.post(reverse('admin-verification-review', args=[certificate.pk]), {'action': 'approve'}, format='json')
            self.assertEqual(result.status_code, 200, result.data)
            self.assertEqual(result.data['document']['documentType'], 'TESDA NC II')
            self.assertFalse(result.data['document']['isPackage'])
            self.worker.refresh_from_db()
            self.assertEqual(self.worker.verification_status, 'Verified')
        for doc in self.old_docs:
            doc.refresh_from_db()
            self.assertEqual(doc.verification_status, 'Verified')
        self.assertEqual(self.worker.documents.count(), 4)
        ensure_eligible_pair(self.owner, self.worker)
        self.client.force_authenticate(self.worker)
        response = self.client.post(reverse('booking-post'), self.payload(), format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(response.status_code, 201, response.data)
        response = self.client.post(reverse('booking-proposals-create', args=[self.booking.pk]),
            {'proposed_rate': '800', 'rate_period': 'day'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        response = self.client.patch(reverse('booking-accept', args=[self.booking.pk]), {}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def test_equivalent_certificate_without_expiry_is_valid_but_past_expiry_removes_verification(self):
        certificate = Document.objects.create(user_profile=self.worker, document_type='tesda_nc2',
            document_url='test/equivalent', verification_status='Verified', valid_until=None)
        today = timezone.localdate(timezone=PH_TIME)
        self.assertEqual(self.worker.verification_status, 'Verified')
        self.assertNotIn(certificate.pk, expired_candidates(self.admin, today).values_list('pk', flat=True))
        certificate.valid_until = today - timedelta(days=1)
        certificate.save()
        self.assertEqual(self.worker.verification_status, 'Unverified')
        self.assertIn(certificate.pk, expired_candidates(self.admin, today).values_list('pk', flat=True))

    def test_certificate_number_is_extracted_displayed_and_reviewer_correctable(self):
        prompt = build_system_prompt('tesda_nc2')
        for field in ('certificate_number', 'qualification_title', 'issuing_body', 'valid_until'):
            self.assertIn(field, prompt)
        certificate = Document.objects.create(user_profile=self.worker, document_type='tesda_nc2',
            document_url='test/certificate', extracted_data={'certificate_number': 'NC-123', 'qualification_title': 'Domestic Work NC II'})
        result = AdminVerificationQueueSerializer(certificate).data
        self.assertEqual(result['documentNumber'], 'NC-123')
        self.assertFalse(result['isPackage'])
        corrected = merge_document_fields(certificate.extracted_data, {'document_number': 'NC-456'}, 'tesda_nc2')
        self.assertEqual(corrected['certificate_number'], 'NC-456')
        self.assertEqual(corrected['qualification_title'], 'Domestic Work NC II')

    @patch('verifications.services.document_upload.cloudinary.uploader.upload')
    def test_homeowners_cannot_upload_a_worker_certificate(self, upload):
        self.client.force_authenticate(self.owner)
        response = self.client.post(reverse('document-upload'),
            {'document_type': 'tesda_nc2', 'document_image': image_file()}, format='multipart')
        self.assertEqual(response.status_code, 400)
        upload.assert_not_called()
