from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from unittest.mock import patch

from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test import skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient, APITestCase

from accounts.models import tbl_user_profile as User
from accounts.workforce import PH_TIME
from verifications.models import tbl_documents as Document, tbl_audit_logs as Audit
from verifications.services import document_processor as processor
from verifications.services.document_fields import CORRECTIONS_KEY
from verifications.testing import AuditTransactionTestCase


class ReviewerCorrectionTests(APITestCase):
    def setUp(self):
        cache.clear(); self.addCleanup(cache.clear)
        self.today = timezone.localdate(timezone=PH_TIME)
        self.admin = User.objects.create_user(email='correct-admin@test.com', username='correct-admin', account_type='Admin')
        self.desk = User.objects.create_user(email='correct-desk@test.com', username='correct-desk', account_type='Barangay', barangay='Pagatpat')
        self.user = User.objects.create_user(email='correct-worker@test.com', username='keep-login', account_type='Kasambahay',
                                            barangay='Brgy. Pagatpat', first_name='Wrongname', last_name='Santos', date_of_birth=date(1998, 1, 1))
        data = {'first_name': 'James', 'last_name': 'Santos', 'full_name': 'James Santos',
                'date_of_birth': '1998-01-01', 'clearance_number': 'WR0NG'}
        self.doc = Document.objects.create(user_profile=self.user, document_type='police_clearance',
                                            document_url='original-scan', verification_status='Pending', document_number='WR0NG',
                                            date_issued=self.today-timedelta(days=30), valid_until=self.today+timedelta(days=150),
                                            extracted_data=data, ocr_raw_text='Original machine text.', ocr_processed_at=timezone.now())
        self.companion = Document.objects.create(user_profile=self.user, document_type='nbi_clearance',
                                                  document_url='valid-nbi', verification_status='Verified', extracted_data=data,
                                                  valid_until=self.today+timedelta(days=150))
        self.url = reverse('admin-document-corrections', kwargs={'document_id': self.doc.pk})
        self.client.force_authenticate(self.admin)

    def details(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def save(self, **changes):
        return self.client.patch(self.url, {'version': self.details()['version'], **changes}, format='json')

    def test_names_birthdates_and_document_fields_save_with_before_after_audit_and_no_approval(self):
        self.client.force_authenticate(self.desk)
        locked = {key: getattr(self.user, key) for key in ('username', 'email', 'password', 'contact_number', 'account_type', 'barangay', 'is_active')}
        response = self.save(profile={'first_name': 'James', 'date_of_birth': '1999-02-03'},
                             document={'first_name': 'James', 'date_of_birth': '1999-02-03', 'document_number': 'CORRECT-123',
                                       'date_issued': str(self.today-timedelta(days=5)), 'valid_until': str(self.today+timedelta(days=175))})
        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db(); self.doc.refresh_from_db(); self.companion.refresh_from_db()
        self.assertEqual(self.user.first_name, 'James')
        self.assertEqual(self.user.date_of_birth, date(1999, 2, 3))
        self.assertEqual({key: getattr(self.user, key) for key in locked}, locked)
        self.assertEqual(self.doc.document_number, 'CORRECT-123')
        self.assertEqual(self.doc.extracted_data['clearance_number'], 'CORRECT-123')
        self.assertEqual(self.doc.extracted_data['full_name'], 'James Santos')
        self.assertEqual(self.doc.verification_status, 'Pending')
        self.assertIsNone(self.doc.verifyBy)
        self.assertEqual(self.companion.verification_status, 'Verified')
        self.assertEqual(self.doc.ocr_match_score, 100)
        self.assertNotIn('first_name', {flag['field'] for flag in self.companion.ocr_discrepancies})
        audit = Audit.objects.get(action='UPDATED', document=self.doc)
        self.assertEqual(audit.actor, self.desk)
        self.assertEqual(audit.target_user, self.user)
        self.assertEqual(audit.previous_status, audit.new_status)
        self.assertIn({'section': 'profile', 'field': 'first_name', 'old': 'Wrongname', 'new': 'James'}, audit.metadata['changes'])
        self.assertIn({'section': 'document', 'field': 'document_number', 'old': 'WR0NG', 'new': 'CORRECT-123'}, audit.metadata['changes'])

    def test_superadmin_can_correct_homeowner_document_without_editing_permanent_id_dates(self):
        self.user.account_type = 'Homeowner'; self.user.barangay = 'Agusan'; self.user.save()
        self.doc.document_type = 'national_id_front'; self.doc.save()
        response = self.save(document={'document_number': 'PHIL-123', 'last_name': 'Cruz'})
        self.assertEqual(response.status_code, 200, response.data)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.extracted_data['philsys_number'], 'PHIL-123')
        self.assertEqual(self.save(document={'valid_until': '2030-01-01'}).status_code, 400)

    def test_authentication_and_exact_barangay_jurisdiction_are_required(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.assertEqual(self.client.patch(self.url, {}, format='json').status_code, 401)
        self.client.force_authenticate(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.client.force_authenticate(self.desk)
        for barangay in ('Agusan', 'Pagatpat Heights', ''):
            User.objects.filter(pk=self.user.pk).update(barangay=barangay)
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.patch(self.url, {}, format='json').status_code, 403)

    def test_locked_and_unknown_fields_are_rejected_even_with_other_valid_edits(self):
        for section, fields in [('profile', ('username', 'email', 'password', 'contact_number', 'barangay', 'is_staff', 'is_active', 'account_type')),
                                ('document', ('verification_status', 'document_type', 'document_url', 'extracted_data', 'ocr_raw_text', 'verifyBy'))]:
            for key in fields:
                response = self.save(**{section: {key: 'tampered', 'first_name': 'Unwanted'}})
                self.assertEqual(response.status_code, 400, (key, response.data))
        self.assertFalse(Audit.objects.filter(action='UPDATED').exists())
        self.user.refresh_from_db(); self.assertEqual(self.user.first_name, 'Wrongname')

    def test_invalid_dates_and_empty_required_names_do_not_save_anything(self):
        for changes in [dict(profile={'date_of_birth': 'not-a-date'}), dict(profile={'first_name': ''}),
                        dict(profile={'date_of_birth': str(self.today+timedelta(days=1))}),
                        dict(document={'date_issued': str(self.today+timedelta(days=1))}),
                        dict(document={'date_issued': str(self.today), 'valid_until': str(self.today-timedelta(days=1))})]:
            self.assertEqual(self.save(**changes).status_code, 400)
        self.assertFalse(Audit.objects.filter(action='UPDATED').exists())

    def test_clearing_optional_values_is_audited_but_noop_creates_no_event(self):
        self.assertEqual(self.save(profile={'first_name': 'Wrongname'}).data['changed'], False)
        self.assertFalse(Audit.objects.filter(action='UPDATED').exists())
        response = self.save(profile={'date_of_birth': None}, document={'document_number': None})
        self.assertEqual(response.status_code, 200)
        self.doc.refresh_from_db(); self.user.refresh_from_db()
        self.assertIsNone(self.user.date_of_birth)
        self.assertIsNone(self.doc.document_number)
        self.assertIsNone(self.doc.extracted_data['clearance_number'])

    def test_stale_form_or_replacement_cannot_overwrite_newer_values_or_duplicate_audits(self):
        version = self.details()['version']
        payload = {'version': version, 'profile': {'first_name': 'James'}}
        self.assertEqual(self.client.patch(self.url, payload, format='json').status_code, 200)
        self.assertEqual(self.client.patch(self.url, payload, format='json').status_code, 409)
        self.assertEqual(Audit.objects.filter(action='UPDATED').count(), 1)
        old = self.details()['version']
        Document.objects.filter(pk=self.doc.pk).update(document_url='renewed-image')
        self.assertEqual(self.client.patch(self.url, {'version': old, 'profile': {'first_name': 'Stale'}}, format='json').status_code, 409)

    def test_historical_document_cannot_be_edited_as_if_it_were_the_latest(self):
        Document.objects.create(user_profile=self.user, document_type='police_clearance', document_url='latest')
        self.assertEqual(self.save(document={'document_number': 'STALE'}).status_code, 409)

    def test_failed_audit_rolls_back_profile_document_and_discrepancy_changes(self):
        with patch('verifications.services.corrections.record_audit_log', return_value=None):
            response = self.save(profile={'first_name': 'James'}, document={'document_number': 'NEW'})
        self.assertEqual(response.status_code, 500)
        self.doc.refresh_from_db(); self.user.refresh_from_db()
        self.assertEqual(self.doc.document_number, 'WR0NG')
        self.assertEqual(self.user.first_name, 'Wrongname')

    def test_late_ocr_cannot_overwrite_a_reviewers_correction(self):
        claimed = processor._claim_document(self.doc.pk, force=True)
        def fetch(_):
            self.assertEqual(self.save(document={'document_number': 'HUMAN-123'}).status_code, 200)
            return Image.new('RGB', (4, 4)), b'image'
        with patch.object(processor, 'fetch_image_from_cloudinary', side_effect=fetch), \
             patch.object(processor, 'is_google_vision_available', return_value=True), \
             patch.object(processor, 'extract_text_with_google_vision', return_value='A readable document with wrong OCR'), \
             patch.object(processor, 'extract_structured_data_from_text', return_value={'clearance_number': 'WRONG-AGAIN'}):
            self.assertFalse(processor._process_claimed_document(claimed))
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.document_number, 'HUMAN-123')
        self.assertEqual(self.doc.ocr_raw_text, 'Original machine text.')

    def test_explicit_reanalysis_preserves_corrected_values_and_auto_retry_is_not_needed(self):
        self.assertEqual(self.save(document={'document_number': 'HUMAN-123', 'date_of_birth': '1999-02-03'}).status_code, 200)
        self.doc.refresh_from_db()
        self.assertFalse(processor.document_needs_retry(self.doc, now=timezone.now()+timedelta(hours=1)))
        with patch.object(processor, 'fetch_image_from_cloudinary', return_value=(Image.new('RGB', (4, 4)), b'image')), \
             patch.object(processor, 'is_google_vision_available', return_value=True), \
             patch.object(processor, 'extract_text_with_google_vision', return_value='A readable document with wrong OCR'), \
             patch.object(processor, 'extract_structured_data_from_text', return_value={'clearance_number': 'WRONG-AGAIN', 'date_of_birth': '2000-01-01'}):
            self.assertTrue(processor.process_document(self.doc.pk, force=True))
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.document_number, 'HUMAN-123')
        self.assertEqual(self.doc.extracted_data['date_of_birth'], '1999-02-03')
        self.assertIn(CORRECTIONS_KEY, self.doc.extracted_data)

    def test_audit_trail_exposes_changes_to_reviewers_only_and_never_other_barangays(self):
        self.save(profile={'first_name': 'James'})
        audit_url = reverse('admin-audit-logs')
        self.client.force_authenticate(self.desk)
        response = self.client.get(audit_url, {'barangay': 'All Barangays'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]['metadata']['changes'][0]['old'], 'Wrongname')
        foreign = User.objects.create_user(email='foreign-desk@test.com', username='foreign-desk', account_type='Barangay', barangay='Agusan')
        self.client.force_authenticate(foreign)
        self.assertEqual(self.client.get(audit_url, {'barangay': 'Pagatpat'}).data, [])
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(audit_url).status_code, 401)


class ReviewerCorrectionConcurrencyTests(AuditTransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_competing_edits_produce_one_change_and_one_audit(self):
        cache.clear()
        admin = User.objects.create_user(email='correction-race-admin@test.com', username='correction-race-admin', account_type='Admin')
        user = User.objects.create_user(email='correction-race-user@test.com', username='correction-race-user', account_type='Homeowner', first_name='Before')
        doc = Document.objects.create(user_profile=user, document_type='national_id_front', document_url='race')
        caller = APIClient(); caller.force_authenticate(admin)
        url = reverse('admin-document-corrections', kwargs={'document_id': doc.pk})
        version = caller.get(url).data['version']
        barrier = Barrier(2)
        def edit(name):
            close_old_connections()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                client = APIClient(); client.force_authenticate(User.objects.get(pk=admin.pk))
                barrier.wait(timeout=10)
                return client.patch(url, {'version': version, 'profile': {'first_name': name}}, format='json').status_code
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ['After One', 'After Two']))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(Audit.objects.filter(document=doc, action='UPDATED').count(), 1)
