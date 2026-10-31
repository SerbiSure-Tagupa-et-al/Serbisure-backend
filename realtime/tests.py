import uuid
from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from Serbisure.asgi import application
from chat.models import tbl_chat_message
from booking.models import tbl_booking, tbl_booking_assignment
from notifications.models import tbl_notification
from realtime.events import publish

User = get_user_model()


@override_settings(REALTIME_ALLOWED_ORIGINS=['http://localhost:3000'])
class RealtimeTests(TransactionTestCase):
    def _fixture_teardown(self):
        # TransactionTestCase truncates all tables after each test. Only the
        # disposable, local test database may temporarily disable this guard;
        # it stays enabled throughout test execution and in application code.
        if connection.vendor != 'postgresql':
            return super()._fixture_teardown()
        if (not connection.settings_dict['NAME'].startswith('test_')
                or connection.settings_dict['HOST'] not in ('localhost', '127.0.0.1', '::1')):
            raise RuntimeError('Audit fixture cleanup requires an isolated local test database.')
        with connection.cursor() as cursor:
            cursor.execute('ALTER TABLE tbl_account_access_audit DISABLE TRIGGER account_access_audit_immutable')
            cursor.execute('ALTER TABLE tbl_penalty_adjustment DISABLE TRIGGER penalty_adjustment_immutable')
        try:
            return super()._fixture_teardown()
        finally:
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE tbl_account_access_audit ENABLE TRIGGER account_access_audit_immutable')
                cursor.execute('ALTER TABLE tbl_penalty_adjustment ENABLE TRIGGER penalty_adjustment_immutable')

    def setUp(self):
        self.user = User.objects.create_user(username='rtadmin', email='rtadmin@example.test', password='Password123!', contact_number='+639123451111', account_type='Admin', is_staff=True)
        self.partner = User.objects.create_user(username='rtworker', email='rtworker@example.test', password='Password123!', contact_number='+639123451112', account_type='Kasambahay')
        self.token = str(AccessToken.for_user(self.user))
        self.partner_token = str(AccessToken.for_user(self.partner))

    async def connect(self, token=None, origin=None, host=None, scheme=None):
        headers = [(b'origin', origin.encode())] if origin else []
        if host:
            headers.append((b'host', host.encode()))
        socket = WebsocketCommunicator(application, '/ws/events/', headers=headers)
        # Reproduce Daphne/runserver: it omits scheme. Uvicorn supplies ws/wss.
        if scheme is not None:
            socket.scope['scheme'] = scheme
        connected, _ = await socket.connect()
        self.assertTrue(connected)
        await socket.send_json_to({'type': 'authenticate', 'token': token or self.token})
        self.assertEqual(await socket.receive_json_from(), {'type': 'ready'})
        return socket

    def test_presence_snapshot_connect_disconnect_and_multiple_tabs(self):
        async def scenario():
            admin = await self.connect()
            await admin.send_json_to({'type': 'presence.watch', 'partner_ids': [str(self.partner.pk)]})
            snapshot = await admin.receive_json_from()
            self.assertEqual(snapshot['type'], 'presence.changed')
            self.assertIs(snapshot['is_online'], False)
            first = await self.connect(self.partner_token)
            self.assertIs((await admin.receive_json_from())['is_online'], True)
            second = await self.connect(self.partner_token)
            self.assertIs((await admin.receive_json_from())['is_online'], True)
            await first.disconnect()
            self.assertIs((await admin.receive_json_from())['is_online'], True)
            await second.disconnect()
            self.assertIs((await admin.receive_json_from())['is_online'], False)
            await admin.send_json_to({'type': 'presence.watch', 'partner_ids': []})
            again = await self.connect(self.partner_token)
            self.assertTrue(await admin.receive_nothing(timeout=0.1))
            await again.disconnect(); await admin.disconnect()
        async_to_sync(scenario)()

    def test_presence_subscriptions_enforce_barangay_and_expose_no_private_messages(self):
        staff = User.objects.create_user(username='presence_staff', email='presence_staff@example.test', password='Password123!', contact_number='+639123451118', account_type='Barangay', barangay='Pagatpat')
        self.partner.barangay = 'Carmen'; self.partner.save(update_fields=['barangay'])
        async def scenario():
            socket = await self.connect(str(AccessToken.for_user(staff)))
            await socket.send_json_to({'type': 'presence.watch', 'partner_ids': [str(self.partner.pk), str(self.user.pk)]})
            event = await socket.receive_json_from()
            self.assertEqual(event['partner_id'], str(self.user.pk))
            worker = await self.connect(self.partner_token)
            self.assertTrue(await socket.receive_nothing(timeout=0.1))
            self.assertNotIn('message_payload', event)
            await worker.disconnect(); await socket.disconnect()
        async_to_sync(scenario)()

    def test_presence_rejects_malformed_or_oversized_watch_requests(self):
        async def scenario():
            for ids in ('all', ['not-a-uuid'], [str(self.partner.pk)] * 101):
                socket = await self.connect()
                await socket.send_json_to({'type': 'presence.watch', 'partner_ids': ids})
                self.assertEqual((await socket.receive_output())['code'], 4400)
                await socket.disconnect()
        async_to_sync(scenario)()

    def test_rest_saved_message_reaches_two_accounts_without_polling_or_exposing_payload(self):
        async def scenario():
            first = await self.connect(origin='http://localhost:3000')
            second = await self.connect(self.partner_token)  # Some native clients omit Origin.
            def send():
                client = APIClient(); client.force_authenticate(user=self.partner)
                return client.post('/api/v1/chat/send/', {'receiver_id': str(self.user.pk), 'message_payload': 'Private text'}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
            response = await database_sync_to_async(send)()
            self.assertEqual(response.status_code, 201, response.data)
            for socket, partner in ((first, self.partner), (second, self.user)):
                event = await socket.receive_json_from()
                self.assertEqual(event['type'], 'chat.changed')
                self.assertEqual(event['partner_id'], str(partner.pk))
                self.assertNotIn('message_payload', event)
            await first.disconnect(); await second.disconnect()
        async_to_sync(scenario)()

    def test_android_origin_receives_admin_and_mobile_peer_messages(self):
        owner = User.objects.create_user(username='rtmobileowner', email='rtmobileowner@example.test', password='Password123!', contact_number='+639123451114', account_type='Homeowner')
        async def scenario():
            worker = await self.connect(self.partner_token, origin='http://192.168.1.8:8000', host='192.168.1.8:8000')
            homeowner = await self.connect(str(AccessToken.for_user(owner)), origin='http://192.168.1.8:8000', host='192.168.1.8:8000')
            admin = await self.connect(origin='http://localhost:3000', host='localhost:8000')
            def send(sender, text):
                client = APIClient(); client.force_authenticate(user=sender)
                return client.post('/api/v1/chat/send/', {'receiver_id': str(self.partner.pk), 'message_payload': text}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid.uuid4()))
            async def next_chat(socket):
                # Sending also emits a notification event; the streams carry both.
                for _ in range(4):
                    event = await socket.receive_json_from(timeout=1)
                    if event['type'] == 'chat.changed':
                        return event
                self.fail('No chat event received')
            for sender, socket, text in ((self.user, admin, 'Admin to mobile'), (owner, homeowner, 'Mobile to mobile')):
                response = await database_sync_to_async(send)(sender, text)
                self.assertEqual(response.status_code, 201, response.data)
                event = await next_chat(worker)
                self.assertEqual(event['type'], 'chat.changed')
                self.assertEqual(event['partner_id'], str(sender.pk))
                self.assertEqual(event['message_id'], str(response.data['data']['chat_message_id']))
                self.assertEqual((await next_chat(socket))['partner_id'], str(self.partner.pk))
            await worker.disconnect(); await homeowner.disconnect(); await admin.disconnect()
        async_to_sync(scenario)()

    def test_secure_endpoint_origin_supported_but_unrelated_origins_still_rejected(self):
        async def scenario():
            socket = await self.connect(origin='https://api.example.test', host='api.example.test', scheme='wss')
            await socket.disconnect()
            for origins in (
                [b'https://attacker.example'],
                [b'https://api.example.test.attacker.example'],
                [b'http://api.example.test'],  # Wrong scheme for WSS.
                [b'null'],
                [b'https://api.example.test', b'https://attacker.example'],
            ):
                headers = [(b'host', b'api.example.test')] + [(b'origin', origin) for origin in origins]
                rejected = WebsocketCommunicator(application, '/ws/events/', headers=headers)
                rejected.scope['scheme'] = 'wss'
                self.assertFalse((await rejected.connect())[0])
                await rejected.disconnect()
            # Matching the endpoint origin never bypasses authentication.
            socket = WebsocketCommunicator(application, '/ws/events/', headers=[(b'host', b'api.example.test'), (b'origin', b'https://api.example.test')])
            socket.scope['scheme'] = 'wss'
            self.assertTrue((await socket.connect())[0])
            await socket.send_json_to({'type': 'authenticate', 'token': 'invalid'})
            self.assertEqual((await socket.receive_output())['code'], 4401)
            await socket.disconnect()
        async_to_sync(scenario)()

    def test_commit_only_delivery_and_private_recipient_groups(self):
        async def scenario():
            socket = await self.connect()
            def rolled_back():
                try:
                    with transaction.atomic():
                        publish([self.user.pk], {'type': 'chat.changed'})
                        raise ValueError('Rollback')
                except ValueError:
                    pass
            await database_sync_to_async(rolled_back)()
            await database_sync_to_async(publish)([self.partner.pk], {'type': 'notifications.changed'})
            self.assertTrue(await socket.receive_nothing(timeout=0.1))
            await socket.disconnect()
        async_to_sync(scenario)()

    def test_feed_viewers_receive_only_committed_public_availability(self):
        async def scenario():
            viewer = await self.connect()  # Not a participant in either booking.
            def create(**extra):
                return tbl_booking.objects.create(poster_id=self.partner, booking_type='short_term',
                    service_category=['Cleaning'], rate_amount=800, rate_period='day',
                    start_time=timezone.now()+timedelta(days=2), **extra)
            public = await database_sync_to_async(create)()
            self.assertEqual(await viewer.receive_json_from(), {
                'type': 'feed.changed', 'booking_id': str(public.pk), 'available': True,
            })
            await database_sync_to_async(create)(recipient_id=self.partner, special_instruction='Private offer terms')
            def rollback():
                try:
                    with transaction.atomic():
                        create()
                        raise ValueError('Rollback')
                except ValueError:
                    pass
            await database_sync_to_async(rollback)()
            self.assertTrue(await viewer.receive_nothing(timeout=0.1))
            def expire():
                public.start_time = timezone.now()-timedelta(seconds=1)
                public.save(update_fields=['start_time'])
            await database_sync_to_async(expire)()
            self.assertEqual(await viewer.receive_json_from(), {
                'type': 'feed.changed', 'booking_id': str(public.pk), 'available': True,
            })
            def cancel():
                public.booking_status = 'Cancelled'
                public.save(update_fields=['booking_status'])
            await database_sync_to_async(cancel)()
            self.assertEqual(await viewer.receive_json_from(), {
                'type': 'feed.changed', 'booking_id': str(public.pk), 'available': False,
            })
            await viewer.disconnect()
        async_to_sync(scenario)()

    def test_invalid_expired_and_inactive_tokens_rejected(self):
        expired = AccessToken.for_user(self.user); expired.set_exp(lifetime=timedelta(seconds=-1))
        inactive_token = AccessToken.for_user(self.partner)
        self.partner.is_active = False; self.partner.save(update_fields=['is_active'])
        async def scenario():
            for token in ('bad-token', str(expired), str(inactive_token)):
                socket = WebsocketCommunicator(application, '/ws/events/')
                self.assertTrue((await socket.connect())[0])
                await socket.send_json_to({'type': 'authenticate', 'token': token})
                self.assertEqual((await socket.receive_output())['code'], 4401)
                await socket.disconnect()
        async_to_sync(scenario)()

    def test_untrusted_origin_and_token_in_url_rejected_before_auth(self):
        async def scenario():
            for url, headers in (('/ws/events/', [(b'origin', b'https://attacker.example')]), ('/ws/events/?token=secret', [])):
                socket = WebsocketCommunicator(application, url, headers=headers)
                self.assertFalse((await socket.connect())[0])
                await socket.disconnect()
        async_to_sync(scenario)()

    def test_client_cannot_join_another_room_or_write_messages_through_socket(self):
        async def scenario():
            for command in ({'type': 'subscribe', 'user_id': str(self.partner.pk)}, {'type': 'chat.send', 'receiver_id': str(self.partner.pk)}, {'type': 'authenticate', 'token': self.partner_token}):
                socket = await self.connect()
                await socket.send_json_to(command)
                self.assertEqual((await socket.receive_output())['code'], 4400)
                await socket.disconnect()
        async_to_sync(scenario)()

    def test_ping_and_frame_limit(self):
        async def scenario():
            socket = await self.connect()
            await socket.send_json_to({'type': 'ping'})
            self.assertEqual(await socket.receive_json_from(), {'type': 'pong'})
            await socket.send_to(text_data='x' * 4097)
            self.assertEqual((await socket.receive_output())['code'], 4400)
            await socket.disconnect()
        async_to_sync(scenario)()

    def test_read_acknowledgment_updates_sender_and_receiver_streams(self):
        msg = tbl_chat_message.objects.create(sender_id=self.partner, receiver_id=self.user, message_payload='Read me')
        async def scenario():
            socket = await self.connect()
            def read():
                client = APIClient(); client.force_authenticate(user=self.user)
                return client.get(f'/api/v1/chat/thread/{self.partner.pk}/')
            self.assertEqual((await database_sync_to_async(read)()).status_code, 200)
            self.assertEqual((await socket.receive_json_from())['type'], 'chat.changed')
            await socket.disconnect()
        async_to_sync(scenario)()
        msg.refresh_from_db(); self.assertTrue(msg.is_read)

    def test_notification_and_typing_are_delivered_to_target_only(self):
        async def scenario():
            socket = await self.connect()
            await database_sync_to_async(tbl_notification.objects.create)(sender_id=self.partner, receiver_id=self.user, notification_message='Booking accepted')
            self.assertEqual((await socket.receive_json_from())['type'], 'notifications.changed')
            def typing():
                client = APIClient(); client.force_authenticate(user=self.partner)
                return client.post('/api/v1/chat/typing/', {'partner_id': str(self.user.pk), 'is_typing': True}, format='json')
            self.assertEqual((await database_sync_to_async(typing)()).status_code, 200)
            self.assertEqual(await socket.receive_json_from(), {'type': 'chat.typing', 'partner_id': str(self.partner.pk), 'is_typing': True})
            await socket.disconnect()
        async_to_sync(scenario)()

    def test_redis_failure_never_rolls_back_persisted_messages(self):
        with patch('realtime.events.get_channel_layer', side_effect=ConnectionError('Unavailable')):
            msg = tbl_chat_message.objects.create(sender_id=self.partner, receiver_id=self.user, message_payload='Saved despite delivery outage')
        self.assertTrue(tbl_chat_message.objects.filter(pk=msg.pk).exists())

    def test_assignment_notifies_the_worker_when_the_listing_has_no_private_recipient(self):
        owner = User.objects.create_user(username='rtowner', email='rtowner@example.test', password='Password123!', contact_number='+639123451113', account_type='Homeowner')
        start = timezone.now() + timedelta(days=2)
        booking = tbl_booking.objects.create(poster_id=owner, booking_type='short_term', work_setup='stay_out', service_category=['Cleaning'], start_time=start, end_time=start + timedelta(hours=8), rate_amount=800, rate_period='day')
        tbl_chat_message.objects.create(sender_id=owner, receiver_id=self.partner, message_payload='Booking offer', booking_id=booking)
        async def scenario():
            socket = await self.connect(self.partner_token)
            await database_sync_to_async(tbl_booking_assignment.objects.create)(booking_id=booking, accepter_id=self.partner)
            events = [await socket.receive_json_from() for _ in range(3)]
            self.assertEqual({event['type'] for event in events}, {'feed.changed', 'booking.changed', 'chat.changed'})
            self.assertEqual(next(event for event in events if event['type'] == 'booking.changed')['booking_id'], str(booking.pk))
            await socket.disconnect()
        async_to_sync(scenario)()


    def test_full_suspension_closes_existing_socket_and_rejects_reconnect(self):
        from chat.moderation import set_suspension
        async def scenario():
            socket = await self.connect(self.partner_token)
            await database_sync_to_async(set_suspension)(self.user, self.partner.pk, 'suspend_account')
            close = await socket.receive_output()
            self.assertEqual(close['type'], 'websocket.close')
            self.assertEqual(close['code'], 4403)
            await socket.disconnect()
            retry = WebsocketCommunicator(application, '/ws/events/')
            self.assertTrue((await retry.connect())[0])
            await retry.send_json_to({'type': 'authenticate', 'token': self.partner_token})
            self.assertEqual((await retry.receive_output())['type'], 'websocket.close')
            await retry.disconnect()
        async_to_sync(scenario)()
