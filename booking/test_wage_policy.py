from decimal import Decimal

from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APITestCase

from .wage_policy import get_minimum_wage_info, validate_rate
from .test_urgent_guards import participants, post


class WageRangeTests(SimpleTestCase):
    def test_exact_boundaries_are_accepted(self):
        for kind, region, values in (
            ('short_term', '', ('600.00', '5000.00')),
            ('long_term', 'Region X', ('6500.00', '50000.00')),
            ('long_term', 'NCR', ('7800.00', '50000.00')),
        ):
            for amount in values:
                with self.subTest(kind=kind, region=region, amount=amount):
                    self.assertEqual(validate_rate(amount, kind, region), Decimal(amount))

    def test_outside_bounds_has_clear_engagement_and_currency(self):
        for kind, region, amount, label, bound in (
            ('short_term', '', '599.99', 'Part-time / Daily', '₱600.00'),
            ('short_term', '', '5000.01', 'Part-time / Daily', '₱5,000.00'),
            ('long_term', 'Region X', '6499.99', 'Full-time / Monthly', '₱6,500.00'),
            ('long_term', 'Region X', '50000.01', 'Full-time / Monthly', '₱50,000.00'),
            ('long_term', 'NCR', '7799.99', 'Full-time / Monthly', '₱7,800.00'),
        ):
            with self.subTest(amount=amount), self.assertRaises(ValidationError) as error:
                validate_rate(amount, kind, region, field='proposed_rate')
            self.assertIn(label, str(error.exception.detail['proposed_rate']))
            self.assertIn(bound, str(error.exception.detail['proposed_rate']))

    @override_settings(SERBISURE_PART_TIME_MAX_RATE='4200.00', SERBISURE_FULL_TIME_MAX_RATE='45000.00')
    def test_caps_are_configurable_and_used_by_validation(self):
        self.assertEqual(get_minimum_wage_info('short_term')['maximum_rate'], '4200.00')
        self.assertEqual(get_minimum_wage_info('long_term', region='NCR')['maximum_rate'], '45000.00')
        with self.assertRaises(ValidationError):
            validate_rate('4200.01', 'short_term')
        with self.assertRaises(ValidationError):
            validate_rate('45000.01', 'long_term', 'NCR')


class WageRangeApiTests(APITestCase):
    def test_endpoint_exposes_minimum_and_maximum_for_each_period(self):
        for kind, region, low, high, period in (
            ('short_term', 'Region X', '600.00', '5000.00', 'day'),
            ('long_term', 'Region X', '6500.00', '50000.00', 'month'),
            ('long_term', 'NCR', '7800.00', '50000.00', 'month'),
        ):
            with self.subTest(kind=kind, region=region):
                res = self.client.get(reverse('booking-minimum-wage'), {'booking_type': kind, 'region': region})
                self.assertEqual(res.status_code, 200, res.data)
                self.assertEqual(res.data['minimum_rate'], low)
                self.assertEqual(res.data['maximum_rate'], high)
                self.assertEqual(res.data[f"max_{'daily' if period == 'day' else 'monthly'}_rate"], high)

    def test_post_proposal_and_acceptance_reject_excess_rates_without_writes(self):
        owner, worker = participants()
        self.client.force_authenticate(owner)
        payload = {'booking_type': 'short_term', 'work_setup': 'stay_out', 'rate_amount': '5000.01',
                   'rate_period': 'day', 'service_category': ['Cleaning']}
        res = self.client.post(reverse('booking-post'), payload, format='json',
                               HTTP_IDEMPOTENCY_KEY='2d853405-59c3-4f30-ae20-c21b88637861')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('₱5,000.00', str(res.data))
        self.client.force_authenticate(worker)
        listing = post(owner, rate_amount='5000.01')
        res = self.client.post(reverse('booking-proposals-create', kwargs={'booking_id': listing.pk}),
                               {'proposed_rate': '5000.01', 'rate_period': 'day'}, format='json')
        self.assertEqual(res.status_code, 400, res.data)
        res = self.client.patch(reverse('booking-accept', kwargs={'booking_id': listing.pk}), {}, format='json')
        self.assertEqual(res.status_code, 400, res.data)
        listing.refresh_from_db()
        self.assertEqual(listing.booking_status, 'Pending')
        self.assertFalse(listing.assignments.exists())
        self.assertFalse(listing.proposals.exists())
