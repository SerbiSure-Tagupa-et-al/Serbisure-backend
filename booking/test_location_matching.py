from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.testing import create_verified_test_user
from .geo import compute_proximity, haversine_km, normalize_barangay, normalize_city
from .test_urgent_guards import post


class LocationGeoUnitTests(APITestCase):
    def test_normalization_and_same_barangay_matching(self):
        self.assertEqual(normalize_barangay('Brgy. Pagatpat'), 'pagatpat')
        self.assertEqual(normalize_barangay('Barangay 01'), 'barangay 1')
        self.assertEqual(normalize_city('City of Cagayan De Oro (CDO)'), 'cagayan de oro')
        dist, label = compute_proximity('Brgy. Pagatpat', 'CDO', 'Pagatpat', 'City of Cagayan De Oro')
        self.assertEqual(dist, 0.0)
        self.assertEqual(label, 'Same Barangay')

    def test_pagatpat_to_carmen_offline_distance(self):
        dist, label = compute_proximity('Pagatpat', 'City of Cagayan De Oro', 'Carmen', 'City of Cagayan De Oro')
        self.assertIsNotNone(dist)
        self.assertGreaterEqual(dist, 3.5)
        self.assertLessEqual(dist, 5.0)
        self.assertEqual(label, f'~{dist:.1f} km away')

    def test_unknown_outside_city_falls_back_to_same_city(self):
        dist, label = compute_proximity('Poblacion', 'Davao City', 'Matina', 'City of Davao')
        self.assertIsNone(dist)
        self.assertEqual(label, 'Same City')
        self.assertGreater(haversine_km(8.4638, 124.6033, 8.4816, 124.6377), 3.0)


class LocationFeedMatchingTests(APITestCase):
    def setUp(self):
        self.worker = create_verified_test_user(
            username='worker_bugo',
            email='worker_bugo@test.com',
            account_type='Kasambahay',
            verification_status='Verified',
            city='City of Cagayan De Oro',
            barangay='Bugo',
        )
        self.owner_bugo = create_verified_test_user(
            username='owner_bugo',
            email='owner_bugo@test.com',
            account_type='Homeowner',
            verification_status='Verified',
            city='City of Cagayan De Oro',
            barangay='Bugo',
        )
        self.owner_agusan = create_verified_test_user(
            username='owner_agusan',
            email='owner_agusan@test.com',
            account_type='Homeowner',
            verification_status='Verified',
            city='City of Cagayan De Oro',
            barangay='Agusan',
        )
        self.owner_puerto = create_verified_test_user(
            username='owner_puerto',
            email='owner_puerto@test.com',
            account_type='Homeowner',
            verification_status='Verified',
            city='City of Cagayan De Oro',
            barangay='Puerto',
        )
        self.owner_pagatpat = create_verified_test_user(
            username='owner_pagatpat',
            email='owner_pagatpat@test.com',
            account_type='Homeowner',
            verification_status='Verified',
            city='City of Cagayan De Oro',
            barangay='Pagatpat',
        )
        self.owner_el_salvador = create_verified_test_user(
            username='owner_es',
            email='owner_es@test.com',
            account_type='Homeowner',
            verification_status='Verified',
            city='City of El Salvador',
            barangay='Poblacion',
        )
        self.owner_opol = create_verified_test_user(
            username='owner_opol',
            email='owner_opol@test.com',
            account_type='Homeowner',
            verification_status='Verified',
            city='Opol',
            barangay='Barra',
        )

        self.job_bugo = post(self.owner_bugo, barangay='Bugo', city='City of Cagayan De Oro')
        self.job_puerto = post(self.owner_puerto, barangay='Puerto', city='City of Cagayan De Oro')
        self.job_agusan = post(self.owner_agusan, barangay='Agusan', city='City of Cagayan De Oro')
        self.job_pagatpat = post(self.owner_pagatpat, barangay='Pagatpat', city='City of Cagayan De Oro')
        self.job_el_salvador = post(self.owner_el_salvador, barangay='Poblacion', city='City of El Salvador')
        self.job_opol = post(self.owner_opol, barangay='Barra', city='Opol')

        self.url = reverse('booking-feed')
        self.client.force_authenticate(self.worker)

    def test_radius_5km_from_bugo_includes_nearby_and_excludes_distant(self):
        res = self.client.get(self.url, {'scope': 'radius', 'radius_km': '5'})
        self.assertEqual(res.status_code, 200)
        ids = {row['booking_id'] for row in res.data}
        self.assertIn(str(self.job_bugo.pk), ids)
        self.assertIn(str(self.job_puerto.pk), ids)
        self.assertIn(str(self.job_agusan.pk), ids)
        self.assertNotIn(str(self.job_pagatpat.pk), ids)
        self.assertNotIn(str(self.job_el_salvador.pk), ids)
        self.assertNotIn(str(self.job_opol.pk), ids)

    def test_same_barangay_returns_only_exact_barangay(self):
        res = self.client.get(self.url, {'scope': 'same_barangay'})
        self.assertEqual(res.status_code, 200)
        ids = [row['booking_id'] for row in res.data]
        self.assertEqual(ids, [str(self.job_bugo.pk)])
        self.assertEqual(res.data[0]['proximity_label'], 'Same Barangay')
        self.assertEqual(res.data[0]['distance_km'], 0.0)

    def test_same_city_returns_all_cdo_and_excludes_other_municipalities(self):
        res = self.client.get(self.url, {'scope': 'same_city'})
        self.assertEqual(res.status_code, 200)
        ids = {row['booking_id'] for row in res.data}
        self.assertEqual(
            ids,
            {str(self.job_bugo.pk), str(self.job_puerto.pk), str(self.job_agusan.pk), str(self.job_pagatpat.pk)},
        )

    def test_sort_nearest_orders_same_barangay_first_then_closest(self):
        res = self.client.get(self.url, {'scope': 'same_city', 'sort': 'nearest'})
        self.assertEqual(res.status_code, 200)
        ids = [row['booking_id'] for row in res.data]
        self.assertEqual(ids[0], str(self.job_bugo.pk))
        self.assertEqual(ids[1], str(self.job_puerto.pk))
        self.assertEqual(ids[2], str(self.job_agusan.pk))
        self.assertEqual(ids[3], str(self.job_pagatpat.pk))

    def test_unknown_origin_city_gracefully_falls_back_to_same_city(self):
        cebu_owner = create_verified_test_user(
            username='owner_cebu',
            email='owner_cebu@test.com',
            account_type='Homeowner',
            verification_status='Verified',
            city='Cebu City',
            barangay='Lahug',
        )
        cebu_job = post(cebu_owner, barangay='Lahug', city='Cebu City')
        res = self.client.get(
            self.url,
            {'scope': 'radius', 'radius_km': '5', 'origin_city': 'Cebu City', 'origin_barangay': 'Banilad'},
        )
        self.assertEqual(res.status_code, 200)
        ids = [row['booking_id'] for row in res.data]
        self.assertEqual(ids, [str(cebu_job.pk)])
        self.assertEqual(res.data[0]['proximity_label'], 'Same City')
