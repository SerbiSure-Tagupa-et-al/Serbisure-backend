import logging

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.db import transaction

logger = logging.getLogger(__name__)
FEED_GROUP = 'feed.public'


def user_group(user_id):
    return f'user.{user_id}'


def publish(user_ids, event):
    """Only emit committed changes; Redis outages must never undo a saved message."""
    ids = {str(user_id) for user_id in user_ids if user_id}
    def send():
        try:
            layer = get_channel_layer()
            for user_id in ids:
                async_to_sync(layer.group_send)(user_group(user_id), {'type': 'user.event', 'event': event})
        except Exception:
            logger.warning('Realtime delivery unavailable; clients will reconcile through REST.')
    transaction.on_commit(send)


def chat_changed(sender_id, receiver_id, message_id=None):
    for user_id, partner_id in ((sender_id, receiver_id), (receiver_id, sender_id)):
        publish([user_id], {'type': 'chat.changed', 'partner_id': str(partner_id),
                            'message_id': str(message_id) if message_id else None})


def publish_feed_change(booking_id):
    """Public availability only: never broadcast private offer terms or participants."""
    def send():
        try:
            from booking.availability import available_public_posts
            available = available_public_posts().filter(pk=booking_id).exists()
            async_to_sync(get_channel_layer().group_send)(FEED_GROUP, {
                'type': 'user.event', 'event': {'type': 'feed.changed', 'booking_id': str(booking_id), 'available': available},
            })
        except Exception:
            logger.warning('Feed update unavailable; clients will reconcile through REST.')
    transaction.on_commit(send)
