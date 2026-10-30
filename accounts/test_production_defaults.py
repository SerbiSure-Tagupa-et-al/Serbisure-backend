from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import tbl_user_profile as User
from .serializers import CustomLoginSerializer
from booking.models import tbl_booking, tbl_booking_assignment
from reviews.models import tbl_review


class ProductionDefaultsTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='audit-admin', email='audit-admin@test.com',
            password='StrongPassword123!', account_type='Admin', contact_number='+639876543201')
        self.owner = User.objects.create_user(username='audit-owner', email='audit-owner@test.com',
            password='StrongPassword123!', account_type='Homeowner', contact_number='+639876543202')
        self.worker = User.objects.create_user(username='audit-worker', email='audit-worker@test.com',
            password='StrongPassword123!', account_type='Kasambahay', contact_number='+639876543203',
            country='', nationality='', city='', street='', province='', zipcode='')
        self.client.force_authenticate(self.admin)

    def booking(self, rating=None, sentiment='Positive', completed=True):
        booking = tbl_booking.objects.create(poster_id=self.owner, service_category=['Cleaning'],
            booking_type='short_term', booking_status='Completed' if completed else 'Pending',
            rate_amount='800', rate_period='day', start_time=timezone.now())
        if completed:
            tbl_booking_assignment.objects.create(booking_id=booking, accepter_id=self.worker)
        if rating is not None:
            tbl_review.objects.create(booking_id=booking, reviewer_id=self.owner, reviewee_id=self.worker,
                rating=rating, nlp_sentiment=sentiment, unstructured_feedback='A real review of this booking.')
        return booking

    def test_directory_uses_actual_counts_and_no_invented_contact_or_compliance(self):
        self.booking(3, 'Negative'); self.booking(5)
        result = self.client.get(reverse('admin-users'))
        self.assertEqual(result.status_code, 200, result.data)
        worker = next(row for row in result.data if row['id'] == str(self.worker.pk))
        self.assertEqual(worker['completedJobs'], 2)
        self.assertEqual(worker['sentimentScore'], {'positive': 50, 'neutral': 0, 'negative': 50})
        self.assertEqual(worker['contactNumber'], self.worker.contact_number)
        self.assertEqual(worker['city'], '')
        self.assertEqual(worker['address'], '')
        self.assertIsNone(worker['ra10361Compliant'])

    def test_empty_directory_reputation_stays_empty(self):
        rows = self.client.get(reverse('admin-users')).data
        worker = next(row for row in rows if row['id'] == str(self.worker.pk))
        self.assertEqual(worker['completedJobs'], 0)
        self.assertEqual(worker['sentimentScore'], {'positive': 0, 'neutral': 0, 'negative': 0})

    def test_lgu_creation_requires_real_contact_and_preserves_explicit_password(self):
        url = reverse('admin-active-barangays')
        for payload in ({'name': 'Audit Desk'}, {'name': 'Audit Desk', 'contact_number': ''}):
            response = self.client.post(url, payload, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(User.objects.filter(account_type='Barangay').exists())
        response = self.client.post(url, {'name': 'Audit Desk', 'contact_number': '+639876543204',
            'password': 'StrongPassword123!'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        desk = User.objects.get(account_type='Barangay')
        self.assertEqual(desk.contact_number, '+639876543204')
        self.assertTrue(desk.check_password('StrongPassword123!'))
        self.assertEqual(desk.city, '')
        self.assertEqual(desk.zipcode, '')

    def test_missing_jwt_demographics_and_postal_code_are_not_invented(self):
        token = CustomLoginSerializer.get_token(self.worker)
        self.assertEqual(token['country'], '')
        self.assertEqual(token['nationality'], '')
        self.worker.nationality = 'Japanese'
        self.assertEqual(CustomLoginSerializer.get_token(self.worker)['nationality'], 'Japanese')
        booking = self.booking(completed=False)
        self.assertEqual(booking.zip_code, '')
