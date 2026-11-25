from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.testing import create_verified_test_user
from .models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal
from .test_urgent_guards import participants, post


class BudgetBidTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner, self.worker = participants()

    def public_post(self, user, **changes):
        body = dict(booking_type='short_term', rate_period='day', work_setup='stay_out', service_category=['Cleaning'],
                    daily_start_time='08:00:00', daily_end_time='17:00:00', region='Region X',
                    city='Cagayan de Oro', barangay='Carmen', street='House 1', zip_code='9000')
        body.update(changes)
        self.client.force_authenticate(user)
        return self.client.post(reverse('booking-post'), body, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))

    def bid(self, listing, actor=None, **body):
        self.client.force_authenticate(actor or self.worker)
        return self.client.post(reverse('booking-proposals-create', kwargs={'booking_id': listing.pk}), body, format='json')

    def respond(self, proposal, action, actor=None):
        self.client.force_authenticate(actor or self.owner)
        return self.client.patch(reverse('booking-proposals-respond', kwargs={'proposal_id': proposal}), {'action': action}, format='json')

    def cap_pair(self):
        for _ in range(3):
            row = post(self.owner, booking_status='Accepted', start_time=timezone.now())
            tbl_booking_assignment.objects.create(booking_id=row, accepter_id=self.worker)

    def test_availability_rate_is_nullable_but_homeowner_budget_is_required(self):
        for body in ({}, {'rate_amount': None}):
            response = self.public_post(self.worker, **body)
            self.assertEqual(response.status_code, 201, response.data)
            row = tbl_booking.objects.get(pk=response.data['data']['booking_id'])
            self.assertIsNone(row.rate_amount)
            self.client.force_authenticate(self.owner)
            detail = self.client.get(reverse('booking-detail', kwargs={'booking_id': row.pk}))
            self.assertEqual(detail.status_code, 200)
            self.assertIsNone(detail.data['rate_amount'])
            self.assertEqual(detail.data['poster_account_type'], 'Kasambahay')
            response = self.public_post(self.owner, **body)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn('rate_amount', response.data)

    def test_legacy_availability_rates_are_preserved_in_storage_and_repost_is_rate_free(self):
        for kind, rate, period in [('short_term', '608', 'day'), ('long_term', '5000', 'month')]:
            legacy = post(self.worker, booking_type=kind, rate_amount=rate, rate_period=period,
                          start_time=timezone.now()-timedelta(days=2))
            self.client.force_authenticate(self.owner)
            feed = self.client.get(reverse('booking-feed'))
            self.assertEqual(feed.status_code, 200, feed.data)
            self.assertEqual(next(row for row in feed.data if row['booking_id'] == str(legacy.pk))['rate_amount'], f'{Decimal(rate):.2f}')
            start = (timezone.now()+timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=30 if kind == 'long_term' else 0, hours=9)
            self.client.force_authenticate(self.worker)
            response = self.client.post(reverse('booking-repost', kwargs={'booking_id': legacy.pk}), {
                'start_time': start.isoformat(), 'end_time': end.isoformat(), 'daily_start_time': '08:00:00',
                'daily_end_time': '17:00:00', 'work_days': ['M', 'T', 'W', 'Th', 'F', 'S', 'Su'],
            }, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
            self.assertEqual(response.status_code, 201, response.data)
            self.assertIsNone(response.data['rate_amount'])
            legacy.refresh_from_db()
            self.assertEqual(legacy.rate_amount, Decimal(rate))

    def test_accept_budget_and_counter_bid_have_rich_metadata_and_no_pending_contract(self):
        for accepted_budget in (True, False):
            listing = post(self.owner)
            response = self.bid(listing, **({'apply_at_posted_rate': True} if accepted_budget else {'proposed_rate': '850', 'rate_period': 'day'}))
            self.assertEqual(response.status_code, 201, response.data)
            proposal = response.data['proposal']
            self.assertEqual(proposal['is_budget_accepted'], accepted_budget)
            self.assertTrue(proposal['is_verified'])
            self.assertTrue(proposal['has_tesda_nc2'])
            self.assertEqual(proposal['jobs_done_count'], 0)
            self.assertIsNone(proposal['recommendation_pct'])
            self.assertEqual(len(self.client.get(reverse('booking-mine')+'?status=pending').data), 0)
        self.assertEqual(len(self.client.get(reverse('booking-my-proposals')).data), 2)

    def test_pending_bid_can_be_withdrawn_only_by_its_proposer_without_penalty(self):
        listing = post(self.owner)
        response = self.bid(listing, apply_at_posted_rate=True)
        proposal = response.data['proposal']['proposal_id']
        self.assertEqual(self.respond(proposal, 'withdraw', self.owner).status_code, 403)
        before = self.worker.penalty_points
        response = self.respond(proposal, 'withdraw', self.worker)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['proposal']['status'], 'Withdrawn')
        self.worker.refresh_from_db()
        listing.refresh_from_db()
        self.assertEqual(self.worker.penalty_points, before)
        self.assertEqual(listing.booking_status, 'Pending')
        self.assertFalse(listing.assignments.exists())
        self.assertEqual(self.respond(proposal, 'withdraw', self.worker).status_code, 400)

    def test_duplicate_pending_counter_bids_are_prevented(self):
        listing = post(self.owner)
        self.assertEqual(self.bid(listing, proposed_rate='850', rate_period='day').status_code, 201)
        self.assertEqual(self.bid(listing, proposed_rate='900', rate_period='day').status_code, 400)
        self.assertEqual(listing.proposals.filter(status='Pending').count(), 1)

    def test_pair_cap_blocks_budget_acceptance_and_bids_but_not_other_pairs_or_monthly(self):
        self.cap_pair()
        response = self.public_post(self.owner, rate_amount='800')
        listing = tbl_booking.objects.get(pk=response.data['data']['booking_id'])
        for body in ({'apply_at_posted_rate': True}, {'proposed_rate': '850', 'rate_period': 'day'}):
            response = self.bid(listing, **body)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(str(response.data['code']), 'short_term_monthly_cap')
        monthly = post(self.owner, booking_type='long_term', rate_period='month', rate_amount='6500')
        self.assertEqual(self.bid(monthly, apply_at_posted_rate=True).status_code, 201)
        other = create_verified_test_user(username='other-budget-owner', email='other-budget-owner@test.com', account_type='Homeowner', verification_status='Verified')
        self.assertEqual(self.bid(post(other), apply_at_posted_rate=True).status_code, 201)

    def test_instant_hire_confirms_an_hours_only_ad_at_the_bid_and_closes_other_bids(self):
        response = self.public_post(self.owner, rate_amount='800')
        listing = tbl_booking.objects.get(pk=response.data['data']['booking_id'])
        chosen = self.bid(listing, proposed_rate='850', rate_period='day').data['proposal']['proposal_id']
        other = create_verified_test_user(username='not-selected-worker', email='not-selected-worker@test.com', account_type='Kasambahay', verification_status='Verified')
        rejected = self.bid(listing, actor=other, apply_at_posted_rate=True).data['proposal']['proposal_id']
        response = self.respond(chosen, 'accept')
        self.assertEqual(response.status_code, 200, response.data)
        listing.refresh_from_db()
        self.assertEqual(listing.booking_status, 'Accepted')
        self.assertEqual(listing.rate_amount, Decimal('850'))
        self.assertEqual(listing.assignments.get().accepter_id_id, self.worker.pk)
        self.assertEqual(tbl_booking_proposal.objects.get(pk=rejected).status, 'Rejected')
        other.refresh_from_db()
        self.assertEqual(other.penalty_points, 0)
        self.assertFalse(tbl_booking_assignment.objects.filter(accepter_id=other).exists())

    def test_hiring_rechecks_a_pair_that_reached_the_cap_after_bidding(self):
        listing = post(self.owner)
        proposal = self.bid(listing, apply_at_posted_rate=True).data['proposal']['proposal_id']
        self.cap_pair()
        response = self.respond(proposal, 'accept')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(str(response.data['code']), 'short_term_monthly_cap')
        self.assertFalse(listing.assignments.exists())
        self.assertEqual(tbl_booking_proposal.objects.get(pk=proposal).status, 'Pending')


    def test_null_availability_serializes_in_feed_profile_and_admin_without_false_wage_flags(self):
        row = post(self.worker, rate_amount=None)
        legacy = post(self.worker, rate_amount='608')
        self.client.force_authenticate(self.owner)
        for url in (reverse('booking-feed'), reverse('booking-profile-pending', kwargs={'user_id': self.worker.pk})):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            item = next(item for item in response.data if item['booking_id'] == str(row.pk))
            self.assertIsNone(item['rate_amount'])
            self.assertEqual(item['poster_account_type'], 'Kasambahay')
        response = self.client.get(reverse('admin-dashboard-activity'))
        self.assertEqual(response.status_code, 200, response.data)
        for booking in (row, legacy):
            item = next(item for item in response.data['bookings'] if item['id'] == str(booking.pk))
            self.assertEqual(item['homeownerName'], 'Unassigned')
            self.assertFalse(item['isBelowMinimumWage'])
            self.assertEqual(item['status'], 'ACTIVE')
        row.refresh_from_db(); legacy.refresh_from_db()
        self.assertIsNone(row.rate_amount)
        self.assertEqual(legacy.rate_amount, Decimal('608'))
