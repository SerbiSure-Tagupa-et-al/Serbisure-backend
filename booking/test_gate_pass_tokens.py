from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from django.core import signing
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import tbl_user_profile as User
from booking.gate_pass import GATE_PASS_MAX_AGE, GATE_PASS_SALT, generate_gate_pass_token
from booking.serializers import BookingDetailSerializer, BookingFeedSerializer
from booking import test_gate_pass as fixtures


class GatePassTokenTests(TestCase):
    setUp = fixtures.GatePassTests.setUp

    def token_url(self, token):
        return reverse('booking-public-pass') + '?' + urlencode({'token': token})

    def test_signature_lasts_86400_seconds_and_expired_pass_discloses_no_booking_details(self):
        issued = 1800000000
        with patch('django.core.signing.time.time', return_value=issued):
            token = generate_gate_pass_token(self.booking.pk)
        for age in (0, 86399, 86400):
            with patch('django.core.signing.time.time', return_value=issued + age), \
                 patch('booking.gate_pass.timezone.now', return_value=self.now):
                response = self.client.get(self.token_url(token))
            self.assertEqual(response.status_code, 200, age)
            self.assertTrue(response.context['entry_allowed'])
        with patch('django.core.signing.time.time', return_value=issued + 86401), \
             patch('booking.views_pass.tbl_booking.objects.select_related') as lookup:
            response = self.client.get(self.token_url(token))
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.context['pass_state'], 'token_expired')
        self.assertContains(response, 'over 24 hours old', status_code=410)
        self.assertNotContains(response, 'Morning Mist Village', status_code=410)
        lookup.assert_not_called()

    def test_unsigned_uuid_missing_tampered_wrong_salt_and_invalid_payloads_never_look_up_a_booking(self):
        token = generate_gate_pass_token(self.booking.pk)
        wrong_salt = signing.dumps(str(self.booking.pk), salt='another.feature')
        urls = [
            reverse('booking-public-pass'), self.token_url(''), self.token_url(str(self.booking.pk)),
            self.token_url(token[:-1] + ('A' if token[-1] != 'A' else 'B')),
            self.token_url(wrong_salt), self.token_url('x' * 513),
            f'/verify/{self.booking.pk}/', f'/verify/{self.booking.pk}/?{urlencode({"token": token})}',
            reverse('booking-public-pass') + '?' + urlencode([('token', token), ('token', token)]),
        ]
        for payload in (None, 123, {}, [], 'not-a-uuid'):
            urls.append(self.token_url(signing.dumps(payload, salt=GATE_PASS_SALT)))
        with patch('booking.views_pass.tbl_booking.objects.select_related') as lookup:
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 404, url)
        lookup.assert_not_called()

    def test_only_participants_can_obtain_a_fresh_pass_url_and_public_feed_never_contains_one(self):
        detail = reverse('booking-detail', kwargs={'booking_id': self.booking.pk})
        for person in (self.owner, self.worker):
            client = APIClient(); client.force_authenticate(person)
            response = client.get(detail)
            self.assertEqual(response.status_code, 200)
            url = response.data['pass_url']
            self.assertNotIn(str(self.booking.pk), url)
            self.assertEqual(urlsplit(url).path, '/verify/')
            token = parse_qs(urlsplit(url).query)['token'][0]
            self.assertEqual(signing.loads(token, salt=GATE_PASS_SALT, max_age=GATE_PASS_MAX_AGE), str(self.booking.pk))
        other = User.objects.create_user(username='pass-outsider', email='outsider@example.test', account_type='Kasambahay')
        client.force_authenticate(other)
        self.assertIsNone(client.get(detail).data['pass_url'])
        self.assertIsNone(BookingDetailSerializer(self.booking).data['pass_url'])
        self.assertNotIn('pass_url', BookingFeedSerializer(self.booking).data)
        client.force_authenticate(None)
        self.assertEqual(client.get(detail).status_code, 401)

    def test_online_refetch_issues_a_new_24_hour_pass_without_extending_the_old_screenshot(self):
        client = APIClient(); client.force_authenticate(self.worker)
        detail = reverse('booking-detail', kwargs={'booking_id': self.booking.pk})
        with patch('django.core.signing.time.time', return_value=1800000000):
            first = client.get(detail).data['pass_url']
        with patch('django.core.signing.time.time', return_value=1800086401):
            fresh = client.get(detail).data['pass_url']
            self.assertEqual(self.client.get(first).status_code, 410)
            self.assertEqual(self.client.get(fresh).status_code, 200)
        self.assertNotEqual(first, fresh)

    @override_settings(DEBUG=True)
    def test_development_pass_keeps_local_http_origin(self):
        client = APIClient(); client.force_authenticate(self.owner)
        response = client.get(reverse('booking-detail', kwargs={'booking_id': self.booking.pk}), HTTP_HOST='192.168.1.14:8000')
        self.assertTrue(response.data['pass_url'].startswith('http://192.168.1.14:8000/verify/?token='))
