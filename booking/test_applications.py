from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta
from unittest.mock import patch

from django.db import close_old_connections
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from chat.models import tbl_chat_message
from notifications.models import tbl_notification
from .models import tbl_booking_proposal
from .test_urgent_guards import BookingTransactionTestCase, participants, post


class JobApplicationTests(APITestCase):
    def setUp(self):
        self.owner, self.worker = participants()
        self.listing = post(self.owner)
        self.url = reverse('booking-proposals-create', kwargs={'booking_id': self.listing.pk})
        self.body = {'apply_at_posted_rate': True, 'message': 'I am interested in this job position'}
        self.client.force_authenticate(self.worker)

    def test_application_uses_posted_rate_sends_message_and_appears_in_applied(self):
        response = self.client.post(self.url, {**self.body, 'proposed_rate': '1', 'rate_period': 'month'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        proposal = tbl_booking_proposal.objects.get()
        self.assertEqual(proposal.proposed_rate, self.listing.rate_amount)
        self.assertEqual(proposal.status, 'Pending')
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.booking_status, 'Pending')
        applied = self.client.get(reverse('booking-my-proposals')).data
        self.assertEqual([row['booking_id'] for row in applied], [str(self.listing.pk)])
        self.assertEqual(tbl_chat_message.objects.filter(is_booking_offer=True).count(), 1)
        self.assertEqual(tbl_chat_message.objects.get(is_booking_offer=False).message_payload, self.body['message'])
        self.assertEqual(tbl_notification.objects.filter(receiver_id=self.owner).count(), 1)

    def test_repeated_application_returns_original_without_duplicate_side_effects(self):
        first = self.client.post(self.url, self.body, format='json')
        for _ in range(3):
            repeated = self.client.post(self.url, self.body, format='json')
            self.assertEqual(repeated.status_code, 200, repeated.data)
            self.assertTrue(repeated.data['already_applied'])
            self.assertEqual(repeated.data['proposal']['proposal_id'], first.data['proposal']['proposal_id'])
        self.assertEqual(tbl_booking_proposal.objects.count(), 1)
        self.assertEqual(tbl_chat_message.objects.count(), 2)
        self.assertEqual(tbl_notification.objects.filter(receiver_id=self.owner).count(), 1)
        self.assertEqual(len(self.client.get(reverse('booking-my-proposals')).data), 1)

    def test_passed_schedule_can_be_applied_to_once_but_cannot_be_accepted(self):
        self.listing.start_time = timezone.now()-timedelta(days=1)
        self.listing.save(update_fields=['start_time'])
        first = self.client.post(self.url, self.body, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(self.client.post(self.url, self.body, format='json').status_code, 200)
        applied = self.client.get(reverse('booking-my-proposals')).data
        self.assertEqual(len(applied), 1)
        self.assertEqual(applied[0]['application_status'], 'applied')
        self.assertTrue(applied[0]['schedule_expired'])
        self.assertEqual(tbl_chat_message.objects.count(), 2)
        self.client.force_authenticate(self.owner)
        rejected = self.client.patch(reverse('booking-proposals-respond', kwargs={'proposal_id': first.data['proposal']['proposal_id']}),
            {'action': 'accept'}, format='json')
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('Please send a new offer in chat', str(rejected.data))
        self.assertFalse(self.listing.assignments.exists())
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.booking_status, 'Pending')

    def test_existing_counter_offer_is_preserved(self):
        first = self.client.post(self.url, {'proposed_rate': '900', 'rate_period': 'day', 'message': 'My counter offer.'}, format='json')
        self.assertEqual(first.status_code, 201, first.data)
        response = self.client.post(self.url, self.body, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(tbl_booking_proposal.objects.get().proposed_rate, 900)
        self.assertEqual(tbl_chat_message.objects.count(), 1)

    def test_closed_unverified_own_service_and_private_posts_cannot_be_applied_to(self):
        self.listing.booking_status = 'Cancelled'
        self.listing.save(update_fields=['booking_status'])
        self.assertEqual(self.client.post(self.url, self.body, format='json').status_code, 400)
        self.listing.booking_status = 'Pending'
        self.listing.save(update_fields=['booking_status'])
        self.listing.recipient_id = self.worker
        self.listing.save(update_fields=['recipient_id'])
        self.assertEqual(self.client.post(self.url, self.body, format='json').status_code, 400)
        self.listing.recipient_id = None
        self.listing.save(update_fields=['recipient_id'])
        self.client.force_authenticate(self.owner)
        service_url = reverse('booking-proposals-create', kwargs={'booking_id': post(self.worker).pk})
        self.assertEqual(self.client.post(service_url, self.body, format='json').status_code, 400)
        self.client.force_authenticate(self.worker)
        from verifications.models import tbl_documents
        tbl_documents.objects.filter(user_profile=self.worker).delete()
        self.assertEqual(self.client.post(self.url, self.body, format='json').status_code, 403)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(self.url, self.body, format='json').status_code, 400)
        self.assertFalse(tbl_booking_proposal.objects.exists())

    def test_invalid_application_flag_is_rejected(self):
        response = self.client.post(self.url, {**self.body, 'apply_at_posted_rate': 'false'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(tbl_booking_proposal.objects.exists())

    def test_message_failure_rolls_back_the_application(self):
        create = tbl_chat_message.objects.create

        def fail_plain_message(**kwargs):
            if not kwargs.get('is_booking_offer'):
                raise RuntimeError('Simulated message failure')
            return create(**kwargs)

        with patch('chat.models.tbl_chat_message.objects.create', side_effect=fail_plain_message):
            with self.assertRaises(RuntimeError):
                self.client.post(self.url, self.body, format='json')
        self.assertFalse(tbl_booking_proposal.objects.exists())
        self.assertFalse(tbl_chat_message.objects.exists())


class ConcurrentJobApplicationTests(BookingTransactionTestCase):
    def test_simultaneous_taps_create_one_application_and_one_message_pair(self):
        owner, worker = participants()
        listing = post(owner)
        url = reverse('booking-proposals-create', kwargs={'booking_id': listing.pk})
        barrier = Barrier(2)

        def apply(_):
            close_old_connections()
            try:
                client = APIClient()
                client.force_authenticate(worker)
                barrier.wait(timeout=10)
                response = client.post(url, {'apply_at_posted_rate': True,
                    'message': 'I am interested in this job position'}, format='json')
                return response.status_code, response.data['proposal']['proposal_id']
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(apply, range(2)))
        self.assertCountEqual([code for code, _ in results], [201, 200])
        self.assertEqual(len({proposal for _, proposal in results}), 1)
        self.assertEqual(tbl_booking_proposal.objects.count(), 1)
        self.assertEqual(tbl_chat_message.objects.count(), 2)
        self.assertEqual(tbl_notification.objects.filter(receiver_id=owner).count(), 1)
