import asyncio
import json
import logging
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings
from accounts.authentication import AccountJWTAuthentication as JWTAuthentication

from .events import user_group, FEED_GROUP
from .presence import allowed_presence_ids, is_online, presence_group, update_presence, PRESENCE_TTL

logger = logging.getLogger(__name__)


def origin_allowed(scope):
    origins = [value.decode('utf-8', errors='replace') for key, value in scope['headers'] if key == b'origin']
    if not origins:
        return True  # Native clients may omit Origin; JWT authentication is still required.
    if len(origins) != 1:
        return False
    if origins[0] in settings.REALTIME_ALLOWED_ORIGINS:
        return True
    # React Native Android automatically sends the endpoint's HTTP(S) origin.
    # Permit only that exact origin, not arbitrary LAN sites or wildcard domains.
    hosts = [value.decode('ascii', errors='replace') for key, value in scope['headers'] if key == b'host']
    # ASGI defaults an omitted scheme to ws. Daphne's runserver omits this field.
    scheme = {'ws': 'http', 'wss': 'https'}.get(scope.get('scheme', 'ws'))
    return bool(scheme and len(hosts) == 1 and origins[0] == f'{scheme}://{hosts[0]}')


@database_sync_to_async
def authenticate(token):
    authentication = JWTAuthentication()
    validated = authentication.get_validated_token(token)
    user = authentication.get_user(validated)
    if not user.is_active:
        raise ValueError('Inactive account')
    return str(user.pk), int(validated['exp'])


@database_sync_to_async
def active_account(user_id):
    from django.contrib.auth import get_user_model
    return get_user_model().objects.filter(pk=user_id, is_active=True, is_account_suspended=False).exists()


class UserEventsConsumer(AsyncWebsocketConsumer):
    """Personal events and authorized presence watches; no message writes over sockets."""
    async def connect(self):
        self.user_id = None
        self.group = None
        self.last_check = 0
        self.window_start = time.monotonic()
        self.frames = 0
        self.expiry_task = None
        self.watched_presence = set()
        self.presence_registered = False
        if not origin_allowed(self.scope):
            logger.warning('WebSocket handshake rejected: untrusted Origin.')
            await self.close(code=4403)
            return
        if self.scope.get('query_string'):
            # Tokens must never appear in proxy access logs or URL history.
            await self.close(code=4400)
            return
        await self.accept()
        self.auth_task = asyncio.create_task(self.close_after(5, 4401))

    async def close_after(self, seconds, code):
        await asyncio.sleep(max(0, seconds))
        await self.close(code=code)

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None or text_data is None or len(text_data.encode('utf-8')) > 4096:
            await self.close(code=4400)
            return
        now = time.monotonic()
        if now - self.window_start >= 60:
            self.window_start, self.frames = now, 0
        self.frames += 1
        if self.frames > 120:
            await self.close(code=4429)
            return
        try:
            content = json.loads(text_data)
            if not isinstance(content, dict):
                raise ValueError()
        except (ValueError, TypeError):
            await self.close(code=4400)
            return
        if not self.user_id:
            if content.get('type') != 'authenticate' or not isinstance(content.get('token'), str):
                await self.close(code=4401)
                return
            try:
                self.user_id, self.expires = await authenticate(content['token'])
            except Exception:
                await self.close(code=4401)
                return
            self.auth_task.cancel()
            self.group = user_group(self.user_id)
            await self.channel_layer.group_add(self.group, self.channel_name)
            await self.channel_layer.group_add(FEED_GROUP, self.channel_name)
            self.expiry_task = asyncio.create_task(self.close_after(self.expires - time.time(), 4401))
            self.last_check = time.monotonic()
            self.presence_registered = True
            await self.broadcast_presence(True)
            await self.send(text_data=json.dumps({'type': 'ready'}))
            return
        if not await self.authorized():
            return
        if content.get('type') == 'ping':
            await self.broadcast_presence(True)
            await self.channel_layer.group_add(self.group, self.channel_name)
            await self.channel_layer.group_add(FEED_GROUP, self.channel_name)
            await self.send(text_data=json.dumps({'type': 'pong'}))
            await self.watch_presence(list(self.watched_presence))
        elif content.get('type') == 'presence.watch':
            try:
                await self.watch_presence(content.get('partner_ids'))
            except (ValueError, TypeError, AttributeError):
                await self.close(code=4400)
        else:
            await self.close(code=4400)

    async def broadcast_presence(self, connected):
        online = await database_sync_to_async(update_presence)(self.user_id, self.channel_name, connected)
        await self.channel_layer.group_send(presence_group(self.user_id), {
            'type': 'user.event', 'event': {'type': 'presence.changed',
                'partner_id': self.user_id, 'is_online': online, 'expires_in': PRESENCE_TTL},
        })

    async def watch_presence(self, requested):
        allowed = await database_sync_to_async(allowed_presence_ids)(self.user_id, requested)
        for partner_id in self.watched_presence - allowed:
            await self.channel_layer.group_discard(presence_group(partner_id), self.channel_name)
        for partner_id in allowed:
            await self.channel_layer.group_add(presence_group(partner_id), self.channel_name)
        self.watched_presence = allowed
        for partner_id in allowed:
            online = await database_sync_to_async(is_online)(partner_id)
            await self.send(text_data=json.dumps({'type': 'presence.changed',
                'partner_id': partner_id, 'is_online': online, 'expires_in': PRESENCE_TTL}))

    async def authorized(self):
        if time.time() >= self.expires:
            await self.close(code=4401)
            return False
        if time.monotonic() - self.last_check >= 30:
            if not await active_account(self.user_id):
                await self.close(code=4401)
                return False
            self.last_check = time.monotonic()
        return True

    async def user_event(self, event):
        if self.user_id and event['event'].get('type') == 'moderation.changed' and not await active_account(self.user_id):
            await self.close(code=4403)
            return
        if self.user_id and await self.authorized():
            if event['event'].get('type') == 'presence.changed':
                partner_id = event['event'].get('partner_id')
                if partner_id not in self.watched_presence:
                    return
                if not await database_sync_to_async(allowed_presence_ids)(self.user_id, [partner_id]):
                    await self.channel_layer.group_discard(presence_group(partner_id), self.channel_name)
                    self.watched_presence.discard(partner_id)
                    return
                # Concurrent workers may enqueue connect/disconnect events in either order.
                event['event']['is_online'] = await database_sync_to_async(is_online)(partner_id)
            await self.send(text_data=json.dumps(event['event']))

    async def disconnect(self, close_code):
        for task in (getattr(self, 'auth_task', None), self.expiry_task):
            if task:
                task.cancel()
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)
            await self.channel_layer.group_discard(FEED_GROUP, self.channel_name)
        for partner_id in self.watched_presence:
            await self.channel_layer.group_discard(presence_group(partner_id), self.channel_name)
        if self.presence_registered:
            await self.broadcast_presence(False)
