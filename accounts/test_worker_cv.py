import csv
from io import StringIO
from decimal import Decimal
from unittest.mock import patch
from django.urls import reverse
from django.core.cache import cache
from rest_framework.test import APITestCase
from .models import tbl_user_profile, tbl_worker_profile
from .serializers import UserRegistrationSerializer
from .cv_service import snapshot, pdf_bytes, csv_bytes
from .cv_presentation import available, days, shifts, money, phone, build
from .cv_service import LABELS
from datetime import date


class WorkerCvTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.worker = tbl_user_profile.objects.create_user(email='cv-worker@test.com', username='cv-worker', password='TestPassword123!', account_type='Kasambahay', first_name='María', last_name='Santos', contact_number='+639123456711', street='PRIVATE STREET', city='Cagayan de Oro', province='Misamis Oriental', date_of_birth='1998-01-01', user_tags=['Cook', 'Child Care', 'Stay-Out'], language='Bisaya, Tagalog')
        self.owner = tbl_user_profile.objects.create_user(email='cv-owner@test.com', username='cv-owner', password='TestPassword123!', account_type='Homeowner', contact_number='+639123456712')
        self.details = tbl_worker_profile.objects.create(user=self.worker, civil_status='Married', number_of_children=6, desired_monthly_rate=Decimal('8000'), experience_level='household')
        self.url = reverse('worker-profile')
        self.cv = reverse('worker-cv', args=[self.worker.pk])
        self.export = reverse('worker-cv-export', args=[self.worker.pk])
        self.client.force_authenticate(self.worker)

    def test_registration_saves_structured_details_atomically(self):
        serializer = UserRegistrationSerializer(data={'first_name': 'Juanita', 'last_name': 'Reyes', 'email': 'cv-new@test.com', 'password': 'StrongPassword123!', 'contact_number': '+639123456713', 'account_type': 'Kasambahay', 'worker_profile': {'civil_status': 'Married', 'number_of_children': 2, 'service_categories': ['Cleaning', 'Child_care', 'Cooking', 'Caregiver', 'Laundry'], 'desired_monthly_rate': '8000.00', 'work_setup': 'stay_out'}})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        user = serializer.save()
        self.assertEqual(user.worker_profile.number_of_children, 2)
        self.assertEqual(user.worker_profile.civil_status, 'Married')
        self.assertEqual(user.worker_profile.service_categories, ['All-around'])
        self.assertEqual(user.worker_profile.desired_monthly_rate, Decimal('8000'))

    def test_family_is_private_unless_worker_shares_it(self):
        self.client.force_authenticate(self.owner)
        public = self.client.get(self.cv)
        self.assertEqual(public.status_code, 200)
        self.assertNotIn('civil_status', public.data['details'])
        self.assertNotIn('number_of_children', public.data['details'])
        self.assertNotIn('+639123456711', str(public.data))
        self.assertNotIn('PRIVATE STREET', str(public.data))
        self.assertNotIn('1998-01-01', str(public.data))
        self.assertEqual(public.data['verification_status'], 'Unverified')
        self.assertIsNone(public.data['performance']['average_rating'])
        self.assertIsNone(public.data['performance']['positive_percentage'])

    def test_zero_and_more_than_five_children_are_valid_and_do_not_restrict_work(self):
        for count in (0, 2, 6, 12):
            response = self.client.patch(self.url, {'number_of_children': count, 'share_family_details': True}, format='json')
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(snapshot(self.worker)['details']['number_of_children'], count)
        self.worker.refresh_from_db()
        self.assertFalse(self.worker.is_restricted)

    def test_invalid_details_are_rejected_without_partial_writes(self):
        for data in ({'number_of_children': -1}, {'number_of_children': 100}, {'number_of_children': 2.5}, {'service_categories': ['Family Driver']}, {'desired_daily_rate': '-1'}, {'desired_monthly_rate': 'NaN'}, {'available_days': ['invalid']}, {'preferred_shifts': ['fake']}):
            with self.subTest(data=data):
                response = self.client.patch(self.url, data, format='json')
                self.assertEqual(response.status_code, 400)
        response = self.client.patch(self.url, {'number_of_children': 3, 'languages': ['invalid']}, format='json')
        self.assertEqual(response.status_code, 400)
        self.details.refresh_from_db()
        self.assertEqual(self.details.number_of_children, 6)

    def test_homeowner_and_anonymous_cannot_edit_worker_details(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.patch(self.url, {'civil_status': 'Single'}).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.cv).status_code, 401)
        self.assertEqual(self.client.get(self.export).status_code, 401)

    def test_latest_edits_appear_and_sharing_can_be_revoked(self):
        result = self.client.patch(self.url, {'service_categories': ['Cleaning'], 'work_setup': 'either', 'share_family_details': True, 'share_contact_details': True, 'languages': ['English']}, format='json')
        self.assertEqual(result.status_code, 200)
        data = self.client.get(self.cv).data
        self.assertEqual(data['details']['civil_status'], 'Married')
        self.assertEqual(data['contact_number'], self.worker.contact_number)
        self.assertEqual(data['languages'], 'English')
        self.client.patch(self.url, {'share_family_details': False, 'share_contact_details': False}, format='json')
        data = self.client.get(self.cv).data
        self.assertNotIn('civil_status', data['details'])
        self.assertIsNone(data['contact_number'])

    @patch('accounts.cv_service._photo', return_value=None)
    def test_pdf_and_csv_exports_use_real_data_and_restrict_csv(self, photo):
        pdf = self.client.get(self.export)
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b'%PDF-'))
        self.assertEqual(pdf['Cache-Control'], 'private, no-store')
        csv_result = self.client.get(self.export, {'file_format': 'csv'})
        self.assertEqual(csv_result.status_code, 200)
        row = next(csv.DictReader(StringIO(csv_result.content.decode('utf-8-sig'))))
        self.assertEqual(row['Name'], 'María Santos')
        self.assertEqual(row['Expected monthly salary'], 'PHP 8,000.00/month')
        self.assertEqual(row['Average rating'], 'No reviews yet')
        self.assertNotIn('Civil status', row)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(self.export, {'file_format': 'csv'}).status_code, 403)
        self.assertEqual(self.client.get(self.export).status_code, 200)
        self.assertEqual(self.client.get(self.export, {'file_format': 'bad'}).status_code, 400)

    def test_csv_formula_injection_is_escaped(self):
        data = snapshot(self.worker)
        data['name'] = '=HYPERLINK("bad")'
        data['details']['preferred_work_areas'] = ['  +cmd']
        row = next(csv.DictReader(StringIO(csv_bytes(data).decode('utf-8-sig'))))
        self.assertTrue(row['Name'].startswith("'="))
        self.assertTrue(row['Preferred work areas'].startswith("'"))

    def test_family_edit_preserves_existing_profile_services(self):
        self.client.patch(self.url, {'number_of_children': 2}, format='json')
        self.worker.refresh_from_db()
        self.assertEqual(self.worker.user_tags, ['Cook', 'Child Care', 'Stay-Out'])

    def test_tag_edits_keep_cv_service_categories_in_sync(self):
        response = self.client.patch(reverse('user-tags'), {'user_tags': ['Laundry', 'Cleaning', 'Stay-In']}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.details.refresh_from_db()
        self.assertEqual(self.details.service_categories, ['Laundry', 'Cleaning'])
        self.assertEqual(self.details.work_setup, 'stay_in')
        self.assertEqual(self.details.civil_status, 'Married')

    def test_uploaded_resume_route_and_profile_fields_are_retired(self):
        response = self.client.patch('/api/v1/accounts/resume/', {}, format='json')
        self.assertEqual(response.status_code, 404)
        self.client.force_authenticate(self.owner)
        public = self.client.get(reverse('public-profile', args=[self.worker.pk]))
        self.assertEqual(public.status_code, 200)
        self.assertNotIn('resume_url', public.data)
        self.assertNotIn('resume_uploaded_at', public.data)

    def test_legacy_profile_has_cv_without_inventing_experience_or_salary(self):
        self.details.delete()
        data = snapshot(self.worker)
        self.assertEqual(data['details']['service_categories'], ['Cooking', 'Child_care'])
        self.assertEqual(data['details']['experience_level'], '')
        self.assertIsNone(data['details']['desired_monthly_rate'])
        self.assertNotIn('number_of_children', data['details'])

    def test_cv_days_and_shifts_are_ordered_grouped_and_deduplicated(self):
        self.assertEqual(days(['Su', 'M', 'T', 'W', 'Th', 'F', 'S', 'M']), 'Every day')
        self.assertEqual(days(['F', 'M', 'W', 'T', 'Th']), 'Weekdays')
        self.assertEqual(days(['Su', 'S']), 'Weekends')
        self.assertEqual(days(['T', 'W', 'Th', 'Su']), 'Tue-Thu, Sun')
        self.assertEqual(days(['F', 'M', 'W']), 'Mon, Wed, Fri')
        self.assertEqual(days(None), '')
        self.assertEqual(shifts(['night', 'afternoon', 'night']), 'Afternoon, Night')
        self.assertEqual(shifts(None), '')

    def test_cv_formatters_preserve_units_and_philippine_dates(self):
        self.assertEqual(money('5500.50', 'month'), 'PHP 5,500.50 / month')
        self.assertEqual(money('800.00', 'day'), 'PHP 800 / day')
        for value in (None, '', 0, 'NaN'):
            self.assertEqual(money(value, 'day'), '')
        self.assertEqual(available('2026-10-12', date(2026, 10, 12)), 'Available now')
        self.assertEqual(available('2026-10-13', date(2026, 10, 12)), 'Oct 13, 2026')
        self.assertEqual(available('2026-02-30'), '')
        self.assertEqual(phone('09265338866'), '+63 926 533 8866')
        self.assertEqual(phone('+639265338866'), '+63 926 533 8866')

    def test_cv_presentation_omits_empty_sections_and_zero_activity(self):
        data = snapshot(self.worker)
        self.assertEqual(data['presentation'], build(data, LABELS))
        self.assertNotIn('Not provided', str(data['presentation']))
        self.assertNotIn('0 completed jobs', str(data['presentation']))
        self.assertNotIn('Background', [item['title'] for item in data['presentation']['sections']])
        self.assertNotIn('Training', [item['title'] for item in data['presentation']['sections']])
        self.assertFalse(data['presentation']['verified'])
        self.assertIn('Verification: Unverified', data['presentation']['trust'])

    def test_cv_presentation_keeps_opted_in_zero_children_and_actual_statistics(self):
        data = snapshot(self.worker)
        data['details'].update(civil_status='Married', number_of_children=0, experience_level='new', experience_duration='over_3')
        data['performance'].update(completed_jobs=3, review_count=2, average_rating=3.5, positive_percentage=0)
        model = build(data, LABELS)
        background = next(item for item in model['sections'] if item['title'] == 'Background')
        self.assertIn({'label': 'Number of children', 'value': '0'}, background['items'])
        experience = next(item for item in model['sections'] if item['title'] == 'Experience')
        self.assertEqual(experience['items'], ['First-time worker'])
        self.assertIn('3 completed jobs · 2 reviews · 3.5/5 stars · 0% positive feedback', model['trust'])

    @patch('accounts.cv_service._photo', return_value=None)
    def test_pdf_handles_long_escaped_text_and_missing_photo(self, photo):
        data = snapshot(self.worker)
        data['name'] = 'María de la Cruz Santos Reyes Villanueva & <script>'
        data['details']['experience_summary'] = 'Text with <script> & symbols ' * 70
        self.assertTrue(pdf_bytes(data).startswith(b'%PDF-'))

    def test_pdf_handles_a_circular_photo(self):
        from PIL import Image
        from io import BytesIO
        image = BytesIO()
        Image.new('RGB', (200, 100), '#ABCDEF').save(image, format='PNG')
        image.seek(0)
        with patch('accounts.cv_service._photo', return_value=image):
            self.assertTrue(pdf_bytes(snapshot(self.worker)).startswith(b'%PDF-'))

    def test_public_work_history_returns_verified_jobs_referrals_and_hides_street(self):
        from datetime import timedelta
        from decimal import Decimal
        from django.utils import timezone
        from booking.models import tbl_booking, tbl_booking_assignment
        from reviews.models import tbl_review

        now = timezone.now()
        # 2 completed jobs with the same homeowner (counts as 1 repeat employer) + 1 pending job (excluded)
        b1 = tbl_booking.objects.create(
            poster_id=self.owner, booking_type='short_term', work_setup='stay_out',
            booking_status='Completed', service_category=['Cleaning'],
            start_time=now - timedelta(days=10), end_time=now - timedelta(days=9),
            street='999 Secret St', barangay='Agusan', city='Cagayan de Oro',
            province='Misamis Oriental', region='Region X', zip_code='9000',
            rate_amount=Decimal('700.00'), rate_period='day',
        )
        tbl_booking_assignment.objects.create(booking_id=b1, accepter_id=self.worker)
        tbl_review.objects.create(
            booking_id=b1, reviewer_id=self.owner, reviewee_id=self.worker,
            rating=5, would_recommend=True, unstructured_feedback='Very punctual and reliable!', nlp_sentiment='Positive',
        )

        b2 = tbl_booking.objects.create(
            poster_id=self.worker, booking_type='long_term', work_setup='stay_in',
            booking_status='Completed', service_category=['Cooking', 'Child_care'],
            start_time=now - timedelta(days=40), end_time=now - timedelta(days=5),
            street='888 Private Rd', barangay='Carmen', city='Cagayan de Oro',
            province='Misamis Oriental', region='Region X', zip_code='9000',
            rate_amount=Decimal('7500.00'), rate_period='month',
        )
        tbl_booking_assignment.objects.create(booking_id=b2, accepter_id=self.owner)

        # Neither participant's history may expose pending or cancelled bookings.
        for status, category in [('Pending', 'Laundry'), ('Cancelled', 'Caregiver')]:
            hidden = tbl_booking.objects.create(
                poster_id=self.owner, booking_type='short_term', work_setup='stay_out',
                booking_status=status, service_category=[category],
                start_time=now + timedelta(days=2), end_time=now + timedelta(days=3),
                street='777 Hidden Ln', floor_number='PRIVATE UNIT 77', barangay='Nazareth', city='Cagayan de Oro',
                province='Misamis Oriental', region='Region X', zip_code='9000',
                rate_amount=Decimal('650.00'), rate_period='day',
            )
            tbl_booking_assignment.objects.create(booking_id=hidden, accepter_id=self.worker)

        self.client.force_authenticate(user=self.owner)
        res = self.client.get(reverse('public-worker-history', args=[self.worker.pk]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['summary']['completed_jobs_count'], 2)
        self.assertEqual(res.data['account_type'], 'Kasambahay')
        self.assertTrue(all(job['partner_role'] == 'Homeowner' for job in res.data['jobs']))
        self.assertEqual(res.data['summary']['recommended_count'], 1)
        self.assertEqual(res.data['summary']['repeat_employers_count'], 1)
        self.assertEqual(len(res.data['jobs']), 2)
        serialized = str(res.data)
        self.assertNotIn('999 Secret St', serialized)
        self.assertNotIn('888 Private Rd', serialized)
        self.assertNotIn('Laundry', serialized)
        self.assertNotIn('Caregiver', serialized)
        self.assertIn('Agusan, Cagayan de Oro', serialized)
        self.assertIn('Carmen, Cagayan de Oro', serialized)

        cv_res = self.client.get(self.cv)
        self.assertEqual(cv_res.status_code, 200)
        self.assertEqual(cv_res.data['performance']['completed_jobs'], 2)
        self.assertEqual(cv_res.data['performance']['recommended_count'], 1)
        self.assertEqual(len(cv_res.data['work_history']), 2)

        # Homeowners receive the same completed history with role-aware partner metadata.
        tbl_review.objects.create(booking_id=b1, reviewer_id=self.worker, reviewee_id=self.owner,
            rating=5, would_recommend=True, unstructured_feedback='Respectful and helpful homeowner.', nlp_sentiment='Positive')
        tbl_review.objects.create(booking_id=b2, reviewer_id=self.worker, reviewee_id=self.owner,
            rating=3, would_recommend=False, unstructured_feedback='The instructions could be clearer.', nlp_sentiment='Neutral')
        self.client.force_authenticate(user=self.worker)
        homeowner_history = self.client.get(reverse('public-worker-history', args=[self.owner.pk]))
        self.assertEqual(homeowner_history.status_code, 200)
        self.assertEqual(homeowner_history.data['summary'], {
            'recommended_count': 1, 'completed_jobs_count': 2, 'repeat_employers_count': 1,
        })
        self.assertEqual(homeowner_history.data['account_type'], 'Homeowner')
        self.assertEqual(len(homeowner_history.data['jobs']), 2)
        for job in homeowner_history.data['jobs']:
            self.assertEqual(job['partner_role'], 'Kasambahay')
            self.assertEqual(job['employer_name'], f'{self.worker.first_name} {self.worker.last_name}')
            self.assertNotIn('street', job)
            self.assertNotIn('floor_number', job)
            self.assertNotIn('floor_unit', job)
            self.assertNotIn('email', job)
            self.assertNotIn('contact_number', job)
        first_job = next(job for job in homeowner_history.data['jobs'] if job['booking_id'] == str(b1.pk))
        self.assertEqual(first_job['area'], 'Agusan, Cagayan de Oro')
        self.assertIs(first_job['would_recommend'], True)
        for secret in ('999 Secret St', '888 Private Rd', '777 Hidden Ln', 'PRIVATE UNIT 77', 'Laundry', 'Caregiver',
                       self.worker.email, self.owner.email, self.worker.contact_number):
            self.assertNotIn(secret, str(homeowner_history.data))

    def test_desk_accounts_have_no_public_booking_history_or_referrals(self):
        for role in ('Admin', 'Barangay'):
            desk = tbl_user_profile.objects.create_user(username=f'history-{role}', email=f'history-{role}@test.com', account_type=role)
            result = self.client.get(reverse('public-worker-history', args=[desk.pk]))
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.data['account_type'], role)
            self.assertEqual(result.data['jobs'], [])
            self.assertEqual(result.data['summary'], {'recommended_count': 0, 'completed_jobs_count': 0, 'repeat_employers_count': 0})
