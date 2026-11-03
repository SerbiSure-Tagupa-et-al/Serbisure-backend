from accounts.testing import create_verified_test_user
import datetime
import uuid
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from booking.models import tbl_booking, tbl_booking_proposal
from notifications.models import tbl_notification

User = get_user_model()


class PublicProfilePostsTests(APITestCase):
    def setUp(self):
        self.owner = create_verified_test_user(
            email='profile-owner@test.com', password='TestPassword123!',
            username='profile-owner', account_type='Homeowner', verification_status='Verified',
        )
        self.worker = create_verified_test_user(
            email='profile-worker@test.com', password='TestPassword123!',
            username='profile-worker', account_type='Kasambahay', verification_status='Verified',
        )
        self.post = self.create_post(self.owner)
        self.url = reverse('booking-profile-pending', kwargs={'user_id': self.owner.id})
        self.client.force_authenticate(self.worker)

    def create_post(self, poster, **kwargs):
        return tbl_booking.objects.create(
            poster_id=poster, booking_type='short_term', rate_period='day', service_category=['Cleaning'],
            start_time=timezone.now() + datetime.timedelta(days=1), rate_amount='600.00',
            city='Cagayan de Oro', barangay='Macasandig', **kwargs,
        )

    def test_only_requested_profiles_pending_posts_are_visible(self):
        self.create_post(self.worker)
        for booking_status in ['Accepted', 'InProgress', 'Completed', 'Cancelled']:
            self.create_post(self.owner, booking_status=booking_status)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([str(item['booking_id']) for item in response.data], [str(self.post.booking_id)])
        self.assertNotIn('contact_number', response.data[0])
        self.assertNotIn('email', response.data[0])

    def test_only_viewers_own_proposal_is_returned(self):
        own = tbl_booking_proposal.objects.create(booking_id=self.post, proposer_id=self.worker, proposed_rate='600.00')
        tbl_booking_proposal.objects.create(booking_id=self.post, proposer_id=self.owner, proposed_rate='700.00')
        response = self.client.get(self.url)
        self.assertEqual(response.data[0]['viewer_proposal'], {'proposal_id': str(own.proposal_id), 'status': 'Pending'})
        self.client.force_authenticate(self.owner)
        response = self.client.get(reverse('booking-profile-pending', kwargs={'user_id': self.worker.id}))
        self.assertEqual(response.data, [])

    def test_authentication_and_inactive_or_missing_profile(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_authenticate(self.worker)
        self.owner.is_active = False
        self.owner.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(self.url).status_code, 404)
        missing = reverse('booking-profile-pending', kwargs={'user_id': uuid.uuid4()})
        self.assertEqual(self.client.get(missing).status_code, 404)

    def test_application_notifies_owner_and_keeps_post_pending(self):
        proposal_url = reverse('booking-proposals-create', kwargs={'booking_id': self.post.booking_id})
        response = self.client.post(proposal_url, {'proposed_rate': '600.00', 'rate_period': 'day', 'message': 'Job application: I am interested.'}, format='json')
        self.assertEqual(response.status_code, 201)
        self.post.refresh_from_db()
        self.assertEqual(self.post.booking_status, 'Pending')
        notice = tbl_notification.objects.get(receiver_id=self.owner, sender_id=self.worker)
        self.assertIn('applied for your job', notice.notification_message)
        self.assertEqual(self.client.get(self.url).data[0]['viewer_proposal']['status'], 'Pending')
        self.assertEqual(self.client.post(proposal_url, {'proposed_rate': '600.00', 'rate_period': 'day'}, format='json').status_code, 400)

    def test_hiring_offer_notifies_worker_and_leaves_service_pending(self):
        service = self.create_post(self.worker)
        self.client.force_authenticate(self.owner)
        response = self.client.post(reverse('booking-proposals-create', kwargs={'booking_id': service.booking_id}), {'proposed_rate': '600.00', 'rate_period': 'day', 'message': 'Hiring offer: Help with cleaning.'}, format='json')
        self.assertEqual(response.status_code, 201)
        service.refresh_from_db()
        self.assertEqual(service.booking_status, 'Pending')
        self.assertIn('sent a hiring offer', tbl_notification.objects.get(receiver_id=self.worker, sender_id=self.owner).notification_message)

