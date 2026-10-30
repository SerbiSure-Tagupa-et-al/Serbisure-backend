from django.urls import reverse
from django.utils import timezone
from django.core.cache import cache
from rest_framework.test import APITestCase
from .models import tbl_user_profile as User
from booking.models import tbl_booking, tbl_booking_assignment
from verifications.models import tbl_documents


class AdminStatisticsTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.sequence = 0
        self.admin = self.user('Admin', '')
        self.worker = self.user('Kasambahay', 'Brgy. Pagatpat', verification_status='Verified')
        self.owner = self.user('Homeowner', 'Pagatpat')
        self.stats = reverse('admin-dashboard-stats')
        self.active = reverse('admin-active-barangays')
        self.client.force_authenticate(self.admin)

    def user(self, role, barangay, **extra):
        self.sequence += 1
        return User.objects.create_user(email=f'stats-{self.sequence}@test.com', username=f'stats-{self.sequence}',
            password='StrongPassword123!', account_type=role, contact_number=f'+63987654{self.sequence:04}',
            barangay=barangay, first_name='Test', last_name='User', **extra)

    def booking(self, poster, accepter, status='Accepted'):
        booking = tbl_booking.objects.create(poster_id=poster, booking_type='short_term', booking_status=status,
            service_category=['Cleaning'], rate_amount='800', rate_period='day', start_time=timezone.now())
        tbl_booking_assignment.objects.create(booking_id=booking, accepter_id=accepter)
        return booking

    def test_resident_addresses_never_create_lgu_accounts_or_demo_counts(self):
        data = self.client.get(self.stats).data
        self.assertEqual(data['metrics']['totalWorkers'], 1)
        self.assertEqual(data['metrics']['totalHomeowners'], 1)
        self.assertEqual(data['barangays'], [{'name': 'Pagatpat', 'status': 'INACTIVE', 'totalRegistered': 2,
            'totalWorkers': 1, 'employed': 0, 'available': 1, 'employmentRatio': 0, 'pending': 0, 'verified': 1, 'rejected': 0, 'expired': 0, 'noDocuments': 1}])
        self.assertEqual(self.client.get(self.active).data['active_lgus'], [])
        self.assertEqual(self.client.get(self.active).data['user_barangays'], ['Pagatpat'])

    def test_real_active_lgu_with_zero_residents_and_deactivation(self):
        desk = self.user('Barangay', 'Canitoan')
        rows = self.client.get(self.stats).data['barangays']
        row = next(row for row in rows if row['name'] == 'Canitoan')
        self.assertEqual(row['status'], 'ACTIVE')
        self.assertEqual(row['employed'], 0)
        self.assertEqual(row['available'], 0)
        desk.is_active = False; desk.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(self.active).data['active_lgus'], [])
        self.assertNotIn('Canitoan', [row['name'] for row in self.client.get(self.stats).data['barangays']])

    def test_verification_counts_people_once_and_requires_both_documents(self):
        for kind in ['nbi_clearance', 'police_clearance']:
            tbl_documents.objects.create(user_profile=self.worker, document_type=kind, document_url='test', verification_status='Pending')
        tbl_documents.objects.create(user_profile=self.owner, document_type='national_id_front', document_url='test', verification_status='Verified')
        row = self.client.get(self.stats).data['barangays'][0]
        self.assertEqual(row['pending'], 2)
        self.assertEqual(row['verified'], 0)
        self.assertEqual(row['pending'] + row['verified'] + row['rejected'] + row['noDocuments'], row['totalRegistered'])

    def test_workforce_ignores_bookings_and_uses_only_the_verified_worker_toggle(self):
        self.booking(self.worker, self.owner)
        self.booking(self.owner, self.worker)
        data = self.client.get(self.stats).data
        self.assertEqual(data['metrics']['totalEmployed'], 0)
        self.assertEqual(data['metrics']['totalAvailable'], 1)
        tbl_booking.objects.update(booking_status='Cancelled')
        self.assertEqual(self.client.get(self.stats).data['metrics']['totalAvailable'], 1)
        self.worker.is_on_job = True; self.worker.save(update_fields=['is_on_job'])
        self.assertEqual(self.client.get(self.stats).data['metrics']['totalEmployed'], 1)

    def test_empty_lgu_coverage_counts_all_residents_and_exact_barangay_matching(self):
        self.user('Kasambahay', 'Pagatpat Heights', street='Pagatpat', verification_status='Verified')
        self.assertEqual(self.client.get(self.stats, {'barangay': 'UNASSIGNED'}).data['metrics']['totalWorkers'], 2)
        self.assertEqual(self.client.get(self.stats, {'barangay': 'Pagatpat'}).data['metrics']['totalWorkers'], 1)
        self.user('Barangay', 'Pagatpat')
        self.assertEqual(self.client.get(self.stats, {'barangay': 'UNASSIGNED'}).data['metrics']['totalWorkers'], 1)

    def test_officer_cannot_override_jurisdiction_or_create_an_lgu(self):
        officer = self.user('Barangay', 'Pagatpat', is_staff=True)
        self.user('Kasambahay', 'Agusan')
        self.client.force_authenticate(officer)
        data = self.client.get(self.stats, {'barangay': 'All Barangays'}).data
        self.assertEqual(data['metrics']['totalWorkers'], 1)
        self.assertEqual([row['name'] for row in data['barangays']], ['Pagatpat'])
        self.assertEqual(self.client.post(self.active, {'name': 'Agusan'}, format='json').status_code, 403)

    def test_admin_statistics_require_real_authenticated_admin(self):
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.client.get(self.stats).status_code, 403)
        self.assertEqual(self.client.get(self.active).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.stats).status_code, 401)
        self.assertEqual(self.client.post(self.active, {'name': 'Fake Barangay'}, format='json').status_code, 401)

    def test_empty_database_statistics_do_not_add_default_barangays(self):
        User.objects.filter(account_type__in=['Homeowner', 'Kasambahay']).delete()
        result = self.client.get(self.stats)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.data['barangays'], [])
        self.assertEqual(result.data['metrics']['totalWorkers'], 0)
        self.assertEqual(result['Cache-Control'], 'private, no-store')
