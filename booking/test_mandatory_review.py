import uuid
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from booking.models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal
from reviews.models import tbl_review
from verifications.models import tbl_documents

User = get_user_model()


class MandatoryPostBookingReviewTests(TestCase):
    def setUp(self):
        self.owner = self._create_verified_user(
            username='owner_rev', email='owner_rev@example.com',
            first_name='Ana', last_name='Santos', account_type='Homeowner',
            contact_number='+639171110001',
        )
        self.owner2 = self._create_verified_user(
            username='owner_rev2', email='owner_rev2@example.com',
            first_name='Ben', last_name='Cruz', account_type='Homeowner',
            contact_number='+639171110002',
        )
        self.worker = self._create_verified_user(
            username='worker_rev', email='worker_rev@example.com',
            first_name='Maria', last_name='Reyes', account_type='Kasambahay',
            contact_number='+639171110003',
        )
        self.worker2 = self._create_verified_user(
            username='worker_rev2', email='worker_rev2@example.com',
            first_name='Rosa', last_name='Lim', account_type='Kasambahay',
            contact_number='+639171110004',
        )
        self.now = timezone.now()
        self.completed = self._create_booking(
            self.owner, status='Completed',
            start=self.now - timedelta(days=3), end=self.now - timedelta(days=2),
            completed_at=self.now - timedelta(days=1),
        )
        tbl_booking_assignment.objects.create(booking_id=self.completed, accepter_id=self.worker)

    def _create_verified_user(self, **kwargs):
        user = User.objects.create_user(password='Password123!', verification_status='Verified', **kwargs)
        tbl_documents.objects.get_or_create(
            user_profile=user, document_type='profile_photo',
            defaults={'document_url': f'seed_photo_{user.username}', 'verification_status': 'Verified'},
        )
        return user

    def _client_for(self, user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _create_booking(self, poster, *, status='Pending', start=None, end=None,
                        recipient=None, completed_at=None):
        start_time = start or (self.now + timedelta(days=2))
        end_time = end or (start_time + timedelta(days=2))
        return tbl_booking.objects.create(
            poster_id=poster, recipient_id=recipient,
            booking_type='short_term', work_setup='stay_out', booking_status=status,
            service_category=['Cleaning'], start_time=start_time, end_time=end_time,
            region='Region X - Northern Mindanao', province='Misamis Oriental',
            city='Cagayan de Oro', barangay='Carmen', street='123 Rizal St', zip_code='9000',
            rate_amount='700.00', rate_period='day', completed_at=completed_at,
        )

    def _post_payload(self):
        return {
            'booking_type': 'short_term', 'work_setup': 'stay_out',
            'service_category': ['Cleaning'],
            'daily_start_time': '08:00', 'daily_end_time': '17:00',
            'region': 'Region X - Northern Mindanao', 'province': 'Misamis Oriental',
            'city': 'Cagayan de Oro', 'barangay': 'Carmen', 'street': '123 Rizal St',
            'zip_code': '9000', 'rate_amount': '700.00', 'rate_period': 'day',
        }

    def _chat_offer_payload(self, recipient):
        start = self.now + timedelta(days=2)
        end = start + timedelta(days=2)
        return {
            **self._post_payload(),
            'recipient_id': str(recipient.pk),
            'start_time': start.isoformat(), 'end_time': end.isoformat(),
            'work_days': ['M', 'T'],
        }

    def test_penalties_endpoint_reports_pending_review_for_both_participants(self):
        owner_res = self._client_for(self.owner).get('/api/v1/booking/penalties/')
        worker_res = self._client_for(self.worker).get('/api/v1/booking/penalties/')
        self.assertEqual(owner_res.status_code, 200)
        self.assertEqual(owner_res.data['pending_review']['booking_id'], str(self.completed.pk))
        self.assertEqual(owner_res.data['pending_review']['reviewee_name'], 'Maria Reyes')
        self.assertEqual(worker_res.status_code, 200)
        self.assertEqual(worker_res.data['pending_review']['booking_id'], str(self.completed.pk))
        self.assertEqual(worker_res.data['pending_review']['reviewee_name'], 'Ana Santos')

    def test_blocks_homeowner_and_kasambahay_on_post_repost_offer_proposal_and_accept(self):
        # 1. Create public post blocked for both owner and worker
        for user, expected_reviewee in ((self.owner, 'Maria Reyes'), (self.worker, 'Ana Santos')):
            res = self._client_for(user).post(
                '/api/v1/booking/post/', self._post_payload(), format='json',
                HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
            )
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.data['code'], 'pending_mandatory_review')
            self.assertEqual(res.data['pending_review']['booking_id'], str(self.completed.pk))
            self.assertEqual(res.data['pending_review']['reviewee_name'], expected_reviewee)

        # 2. Repost expired post blocked
        expired_post = self._create_booking(
            self.owner, status='Pending',
            start=self.now - timedelta(days=2), end=self.now - timedelta(days=1),
        )
        repost_res = self._client_for(self.owner).post(
            f'/api/v1/booking/{expired_post.pk}/repost/',
            {
                'start_time': (self.now + timedelta(days=2)).isoformat(),
                'end_time': (self.now + timedelta(days=3)).isoformat(),
                'daily_start_time': '08:00', 'daily_end_time': '17:00', 'work_days': ['M'],
            },
            format='json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(repost_res.status_code, 403)
        self.assertEqual(repost_res.data['code'], 'pending_mandatory_review')

        # 3. Chat booking offer blocked for owner
        offer_res = self._client_for(self.owner).post(
            '/api/v1/booking/chat-offers/', self._chat_offer_payload(self.worker2),
            format='json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(offer_res.status_code, 403)
        self.assertEqual(offer_res.data['code'], 'pending_mandatory_review')

        # 4. Proposal / apply blocked for worker (both posted-rate apply and custom bid)
        owner2_post = self._create_booking(self.owner2)
        apply_res = self._client_for(self.worker).post(
            f'/api/v1/booking/{owner2_post.pk}/proposals/',
            {'apply_at_posted_rate': True, 'message': 'Interested'},
            format='json',
        )
        self.assertEqual(apply_res.status_code, 403)
        self.assertEqual(apply_res.data['code'], 'pending_mandatory_review')

        bid_res = self._client_for(self.worker).post(
            f'/api/v1/booking/{owner2_post.pk}/proposals/',
            {'proposed_rate': '800.00', 'rate_period': 'day', 'message': 'My bid'},
            format='json',
        )
        self.assertEqual(bid_res.status_code, 403)
        self.assertEqual(bid_res.data['code'], 'pending_mandatory_review')

        # 5. Direct accept blocked for worker
        private_offer = self._create_booking(self.owner2, recipient=self.worker)
        accept_res = self._client_for(self.worker).patch(f'/api/v1/booking/{private_offer.pk}/accept/')
        self.assertEqual(accept_res.status_code, 403)
        self.assertEqual(accept_res.data['code'], 'pending_mandatory_review')

        # 6. Accepting a proposal blocked for owner, while rejecting a proposal stays allowed
        owner_post = self._create_booking(self.owner)
        proposal = tbl_booking_proposal.objects.create(
            booking_id=owner_post, proposer_id=self.worker2, proposed_rate='700.00', message='Applying',
        )
        accept_prop_res = self._client_for(self.owner).patch(
            f'/api/v1/booking/proposals/{proposal.pk}/respond/', {'action': 'accept'}, format='json',
        )
        self.assertEqual(accept_prop_res.status_code, 403)
        self.assertEqual(accept_prop_res.data['code'], 'pending_mandatory_review')

        reject_prop_res = self._client_for(self.owner).patch(
            f'/api/v1/booking/proposals/{proposal.pk}/respond/', {'action': 'reject'}, format='json',
        )
        self.assertEqual(reject_prop_res.status_code, 200)

    def test_option_a_unblocked_actor_can_accept_existing_proposal(self):
        # Owner2 has no pending reviews; worker has an unreviewed completed job with owner1.
        owner2_post = self._create_booking(self.owner2)
        proposal = tbl_booking_proposal.objects.create(
            booking_id=owner2_post, proposer_id=self.worker, proposed_rate='700.00', message='Earlier bid',
        )
        res = self._client_for(self.owner2).patch(
            f'/api/v1/booking/proposals/{proposal.pk}/respond/', {'action': 'accept'}, format='json',
        )
        self.assertEqual(res.status_code, 200)
        owner2_post.refresh_from_db()
        self.assertEqual(owner2_post.booking_status, 'Accepted')

    @patch('reviews.views.analyze_feedback', return_value={'label': 'Positive', 'scores': {'Positive': 0.99}})
    def test_submitting_review_unlocks_only_that_participant(self, _mock_sentiment):
        # Owner omitting would_recommend is blocked and stays locked
        missing_rec = self._client_for(self.owner).post(
            '/api/v1/reviews/create/',
            {
                'booking_id': str(self.completed.pk),
                'rating': 5,
                'unstructured_feedback': 'Very reliable and thorough cleaning service!',
            },
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(missing_rec.status_code, 400)

        # Owner submits complete review with would_recommend -> owner unlocked, worker remains locked
        rev_res = self._client_for(self.owner).post(
            '/api/v1/reviews/create/',
            {
                'booking_id': str(self.completed.pk),
                'rating': 5,
                'would_recommend': True,
                'unstructured_feedback': 'Very reliable and thorough cleaning service!',
            },
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(rev_res.status_code, 201)

        owner_post_res = self._client_for(self.owner).post(
            '/api/v1/booking/post/', self._post_payload(), format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(owner_post_res.status_code, 201)

        worker_post_res = self._client_for(self.worker).post(
            '/api/v1/booking/post/', self._post_payload(), format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(worker_post_res.status_code, 403)
        self.assertEqual(worker_post_res.data['code'], 'pending_mandatory_review')

        # An incomplete worker review must not release the mandatory review lock.
        incomplete_worker_review = self._client_for(self.worker).post(
            '/api/v1/reviews/create/',
            {'booking_id': str(self.completed.pk), 'rating': 5,
             'unstructured_feedback': 'Kind homeowner and very clear instructions.'},
            format='json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(incomplete_worker_review.status_code, 400)
        self.assertIn('would_recommend', incomplete_worker_review.data)
        self.assertFalse(tbl_review.objects.filter(booking_id=self.completed, reviewer_id=self.worker).exists())
        still_locked = self._client_for(self.worker).post(
            '/api/v1/booking/post/', self._post_payload(), format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(still_locked.status_code, 403)
        self.assertEqual(still_locked.data['code'], 'pending_mandatory_review')

        # Worker submits the full review -> worker unlocked too.
        worker_rev_res = self._client_for(self.worker).post(
            '/api/v1/reviews/create/',
            {
                'booking_id': str(self.completed.pk),
                'rating': 5,
                'unstructured_feedback': 'Kind homeowner and very clear instructions.',
                'would_recommend': True,
            },
            format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(worker_rev_res.status_code, 201)

        worker_post_res2 = self._client_for(self.worker).post(
            '/api/v1/booking/post/', self._post_payload(), format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(worker_post_res2.status_code, 201)

    def test_legacy_completed_booking_without_assignment_does_not_lock_user(self):
        # Owner2 has an orphan Completed booking with no tbl_booking_assignment row
        self._create_booking(self.owner2, status='Completed', completed_at=self.now - timedelta(days=5))
        res = self._client_for(self.owner2).post(
            '/api/v1/booking/post/', self._post_payload(), format='json',
            HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()),
        )
        self.assertEqual(res.status_code, 201)
