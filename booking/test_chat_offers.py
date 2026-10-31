from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from django.core import signing
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase
from booking.models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal
from chat.models import tbl_chat_message
from booking.services import PH_TIME
from booking.gate_pass import GATE_PASS_SALT, GATE_PASS_MAX_AGE

User = get_user_model()


class ChatOfferTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email='chat-owner@test.com', username='chat-owner', password='test', account_type='Homeowner', verification_status='Verified')
        self.worker = User.objects.create_user(email='chat-worker@test.com', username='chat-worker', password='test', account_type='Kasambahay', verification_status='Verified')
        self.other = User.objects.create_user(email='chat-other@test.com', username='chat-other', password='test', account_type='Kasambahay', verification_status='Verified')
        self.key = str(uuid4())

    def payload(self, recipient):
        start = (timezone.now().astimezone(PH_TIME)+timedelta(days=2)).replace(hour=8, minute=0, second=0, microsecond=0)
        return dict(recipient_id=str(recipient.pk), booking_type='long_term', work_setup='stay_out',
            service_category=['Cleaning'], rate_amount='8000', rate_period='month',
            start_time=start.isoformat(), end_time=(start+timedelta(days=30, hours=9)).isoformat(),
            daily_start_time='08:00:00', daily_end_time='17:00:00', work_days=[['M', 'T', 'W', 'Th', 'F', 'S', 'Su'][start.weekday()]],
            region='Region X', street='Test agreed work address')

    def send(self, sender, recipient, **updates):
        self.client.force_authenticate(sender)
        data = self.payload(recipient)
        data.update(updates)
        return self.client.post(reverse('booking-chat-offer'), data, format='json', HTTP_IDEMPOTENCY_KEY=self.key)

    def test_either_role_can_send_and_only_counterpart_can_confirm(self):
        for sender, recipient in ((self.owner, self.worker), (self.worker, self.owner)):
            self.key = str(uuid4())
            response = self.send(sender, recipient)
            self.assertEqual(response.status_code, 201, response.data)
            row = tbl_booking.objects.get(pk=response.data['booking']['booking_id'])
            self.assertEqual(row.booking_status, 'Pending')
            self.assertEqual(row.work_days, self.payload(recipient)['work_days'])
            self.assertEqual(str(row.daily_start_time), '08:00:00')
            self.assertFalse(response.data['chat_message']['booking_offer']['can_respond'])
            pass_url = response.data['chat_message']['booking_offer']['booking']['pass_url']
            self.assertEqual(signing.loads(parse_qs(urlsplit(pass_url).query)['token'][0],
                salt=GATE_PASS_SALT, max_age=GATE_PASS_MAX_AGE), str(row.pk))
            self.assertEqual(self.client.patch(reverse('booking-accept', kwargs={'booking_id': row.pk})).status_code, 400)
            self.client.force_authenticate(self.other)
            self.assertEqual(self.client.patch(reverse('booking-accept', kwargs={'booking_id': row.pk})).status_code, 403)
            self.assertEqual(self.client.get(reverse('booking-detail', kwargs={'booking_id': row.pk})).status_code, 404)
            self.assertEqual(self.client.get(reverse('booking-feed')).data, [])
            recommendations = self.client.get(reverse('booking-recommendations')).data.get('recommendations', [])
            self.assertNotIn(str(row.pk), [item.get('booking_id') for item in recommendations])
            self.assertEqual(self.client.get(reverse('booking-profile-pending', kwargs={'user_id': sender.pk})).data, [])
            self.client.force_authenticate(recipient)
            thread = self.client.get(reverse('chat-thread', kwargs={'partner_id': sender.pk})).data
            messages = thread['data'] if isinstance(thread, dict) else thread
            self.assertTrue(messages[-1]['booking_offer']['can_respond'])
            self.assertTrue(messages[-1]['booking_offer']['booking']['pass_url'])
            self.assertEqual(self.client.patch(reverse('booking-accept', kwargs={'booking_id': row.pk})).status_code, 200)
            row.refresh_from_db()
            self.assertEqual(row.booking_status, 'Accepted')
            self.assertEqual(row.assignments.get().accepter_id, recipient)
            thread = self.client.get(reverse('chat-thread', kwargs={'partner_id': sender.pk})).data
            messages = thread['data'] if isinstance(thread, dict) else thread
            self.assertEqual(messages[-1]['booking_offer']['status'], 'Confirmed')
            self.assertTrue(messages[-1]['booking_offer']['booking']['pass_url'])

    def test_idempotency_is_durable_and_changed_terms_cannot_reuse_key(self):
        self.client.force_authenticate(self.owner)
        data = self.payload(self.worker)
        response = self.client.post(reverse('booking-chat-offer'), data, format='json', HTTP_IDEMPOTENCY_KEY=self.key)
        self.assertEqual(response.status_code, 201)
        repeated = self.client.post(reverse('booking-chat-offer'), data, format='json', HTTP_IDEMPOTENCY_KEY=self.key)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(response.data['booking']['booking_id'], repeated.data['booking']['booking_id'])
        self.assertEqual(tbl_booking.objects.count(), 1)
        self.assertEqual(tbl_chat_message.objects.count(), 1)
        data['rate_amount'] = '9000'
        self.assertEqual(self.client.post(reverse('booking-chat-offer'), data, format='json', HTTP_IDEMPOTENCY_KEY=self.key).status_code, 400)

    def source_post(self, poster, **updates):
        values = dict(poster_id=poster, booking_type='short_term', work_setup='stay_out',
            service_category=['Cleaning'], rate_amount='800', rate_period='day', region='Region X',
            start_time=timezone.now()+timedelta(days=2))
        values.update(updates)
        return tbl_booking.objects.create(**values)

    def test_invalid_key_and_recipient_have_separate_messages(self):
        self.client.force_authenticate(self.owner)
        url = reverse('booking-chat-offer')
        response = self.client.post(url, self.payload(self.worker), format='json',
            HTTP_IDEMPOTENCY_KEY='booking-offer-123-random')
        self.assertEqual(response.status_code, 400)
        self.assertIn('request key', str(response.data))
        self.assertNotIn('recipient', str(response.data))
        for recipient in (None, '', 'not-a-uuid'):
            data = {**self.payload(self.worker), 'recipient_id': recipient}
            response = self.client.post(url, data, format='json', HTTP_IDEMPOTENCY_KEY=self.key)
            self.assertEqual(response.status_code, 400)
            self.assertIn('recipient_id', response.data)
        self.assertFalse(tbl_booking.objects.exists())

    def test_legacy_source_parameter_is_ignored_and_never_links_bookings(self):
        listing = self.source_post(self.owner, start_time=timezone.now()-timedelta(days=1))
        response = self.send(self.owner, self.worker, source_post_id=str(listing.pk))
        self.assertEqual(response.status_code, 201, response.data)
        listing.refresh_from_db()
        self.assertEqual(listing.booking_status, 'Pending')
        self.assertNotIn('source_post', {field.name for field in tbl_booking._meta.fields})
        self.assertFalse(listing.assignments.exists())

    def test_invalid_terms_and_message_failure_do_not_leave_partial_offers(self):
        for field, value in [('rate_amount', '100'), ('work_days', []), ('street', ''), ('daily_end_time', '07:00:00')]:
            response = self.send(self.owner, self.worker, **{field: value})
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(tbl_booking.objects.count(), 0)
        with patch('chat.models.tbl_chat_message.objects.create', side_effect=RuntimeError('database failure')):
            with self.assertRaises(RuntimeError):
                self.send(self.owner, self.worker)
        self.assertEqual(tbl_booking.objects.count(), 0)
        self.assertEqual(tbl_chat_message.objects.count(), 0)

    def test_private_receiver_can_decline_and_legacy_text_never_becomes_evidence(self):
        response = self.send(self.owner, self.worker)
        row_id = response.data['booking']['booking_id']
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.client.patch(reverse('booking-cancel', kwargs={'booking_id': row_id}), {'reason': 'Other'}, format='json').status_code, 200)
        self.assertEqual(self.client.patch(reverse('booking-accept', kwargs={'booking_id': row_id})).status_code, 400)
        legacy = tbl_chat_message.objects.create(sender_id=self.owner, receiver_id=self.worker,
            message_payload='[BOOKING]: {"isConfirmed":true,"salary":"999999"}')
        from chat.serializers import ChatMessageSerializer
        self.assertIsNone(ChatMessageSerializer(legacy).data['booking_offer'])

    def test_existing_post_proposals_link_to_chat_and_reject_competitors(self):
        row = tbl_booking.objects.create(poster_id=self.owner, booking_type='short_term', work_setup='stay_out',
            service_category=['Cleaning'], rate_amount='800', rate_period='day', region='Region X',
            start_time=timezone.now()+timedelta(days=2))
        competitor = tbl_booking_proposal.objects.create(booking_id=row, proposer_id=self.other, proposed_rate='800')
        self.client.force_authenticate(self.worker)
        response = self.client.post(reverse('booking-proposals-create', kwargs={'booking_id': row.pk}),
            {'proposed_rate': '900', 'rate_period': 'day'}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        message = tbl_chat_message.objects.get(booking_proposal_id=response.data['proposal']['proposal_id'])
        self.assertTrue(message.is_booking_offer)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.patch(reverse('booking-proposals-respond', kwargs={'proposal_id': message.booking_proposal_id}), {'action': 'accept'}, format='json').status_code, 200)
        competitor.refresh_from_db()
        self.assertEqual(competitor.status, 'Rejected')
        self.assertEqual(tbl_booking_assignment.objects.get(booking_id=row).accepter_id, self.worker)
        self.assertEqual(self.client.get(reverse('booking-my-proposals')).data, [])
        self.client.force_authenticate(self.worker)
        self.assertEqual(self.client.get(reverse('booking-my-proposals')).data[0]['booking_id'], str(row.pk))
        self.client.force_authenticate(self.other)
        history = self.client.get(reverse('booking-my-proposals')).data
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['application_status'], 'not_selected')

    def test_restrictions_are_rechecked_at_acceptance_and_offer_stays_pending_on_error(self):
        response = self.send(self.owner, self.worker)
        row_id = response.data['booking']['booking_id']
        self.worker.is_restricted = True
        self.worker.save(update_fields=['is_restricted'])
        self.client.force_authenticate(self.worker)
        response = self.client.patch(reverse('booking-accept', kwargs={'booking_id': row_id}))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(tbl_booking.objects.get(pk=row_id).booking_status, 'Pending')
        self.assertFalse(tbl_booking_assignment.objects.exists())

    def test_private_terms_cannot_be_changed_or_forwarded_to_another_user(self):
        response = self.send(self.owner, self.worker)
        row_id = response.data['booking']['booking_id']
        self.assertEqual(self.client.patch(reverse('booking-rate-review', kwargs={'booking_id': row_id}),
            {'rate_amount': '9000', 'rate_period': 'month'}, format='json').status_code, 400)
        response = self.client.post(reverse('chat-send'), {'receiver_id': str(self.other.pk),
            'booking_id': row_id, 'message_payload': 'Forwarded private terms'}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
        self.assertEqual(response.status_code, 400, response.data)

    def test_inconsistent_timezone_terms_are_rejected(self):
        response = self.send(self.owner, self.worker, daily_start_time='09:00:00')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(tbl_booking.objects.exists())
