from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework import status
from django.urls import reverse
from accounts.models import tbl_user_profile
from notifications.models import tbl_notification

class NotificationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = tbl_user_profile.objects.create_user(
            username='alice_notif',
            email='alice@example.com',
            password='StrongPassword123!',
            first_name='Alice',
            last_name='Smith',
            account_type='Homeowner'
        )
        self.sender = tbl_user_profile.objects.create_user(
            username='bob_notif',
            email='bob@example.com',
            password='StrongPassword123!',
            first_name='Bob',
            last_name='Builder',
            account_type='Kasambahay'
        )
        self.client.force_authenticate(user=self.user)

        self.notif1 = tbl_notification.objects.create(
            sender_id=self.sender,
            receiver_id=self.user,
            notification_message='Bob sent you a booking request.',
            notification_state='Unread'
        )
        self.notif2 = tbl_notification.objects.create(
            sender_id=self.sender,
            receiver_id=self.user,
            notification_message='Your service has been confirmed.',
            notification_state='Unread'
        )

    def test_list_notifications_authenticated(self):
        url = reverse('notification-list')
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['unread_count'], 2)
        self.assertEqual(len(res.data['notifications']), 2)

    def test_mark_single_notification_read(self):
        url = reverse('notification-mark-read', kwargs={'pk': self.notif1.notification_id})
        res = self.client.patch(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.notif1.refresh_from_db()
        self.assertEqual(self.notif1.notification_state, 'Read')

    def test_mark_all_notifications_read(self):
        url = reverse('notification-mark-all-read')
        res = self.client.patch(url)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data['marked_count'], 2)
        self.notif1.refresh_from_db()
        self.notif2.refresh_from_db()
        self.assertEqual(self.notif1.notification_state, 'Read')
        self.assertEqual(self.notif2.notification_state, 'Read')

    def test_unauthenticated_access_denied(self):
        self.client.logout()
        url = reverse('notification-list')
        res = self.client.get(url)
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
