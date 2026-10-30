from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from booking.models import tbl_booking, tbl_booking_proposal, tbl_booking_assignment
from chat.models import tbl_chat_message, tbl_chat_reaction
from notifications.models import tbl_notification
from .events import chat_changed, publish


@receiver(post_save, sender=tbl_chat_message)
def message_saved(sender, instance, raw=False, **kwargs):
    if not raw:
        chat_changed(instance.sender_id_id, instance.receiver_id_id, instance.pk)


@receiver(post_save, sender=tbl_chat_reaction)
@receiver(post_delete, sender=tbl_chat_reaction)
def reaction_changed(sender, instance, **kwargs):
    message = instance.message
    chat_changed(message.sender_id_id, message.receiver_id_id, message.pk)


@receiver(post_save, sender=tbl_notification)
@receiver(post_delete, sender=tbl_notification)
def notification_changed(sender, instance, **kwargs):
    publish([instance.receiver_id_id], {'type': 'notifications.changed'})


@receiver(post_save, sender=tbl_booking)
@receiver(post_save, sender=tbl_booking_proposal)
@receiver(post_save, sender=tbl_booking_assignment)
def booking_changed(sender, instance, raw=False, **kwargs):
    if raw:
        return
    booking = instance if sender is tbl_booking else instance.booking_id
    if sender in (tbl_booking, tbl_booking_assignment):
        from .events import publish_feed_change
        public_id = booking.pk if not booking.recipient_id_id else None
        if public_id:
            publish_feed_change(public_id)
    participants = {booking.poster_id_id, booking.recipient_id_id}
    participants.update(booking.assignments.values_list('accepter_id_id', flat=True))
    participants.update(booking.proposals.values_list('proposer_id_id', flat=True))
    if sender is tbl_booking_proposal:
        participants.add(instance.proposer_id_id)
    publish(participants, {'type': 'booking.changed', 'booking_id': str(booking.pk)})
    # Contract acceptance can change several existing offer cards without saving their messages.
    for message in tbl_chat_message.objects.filter(booking_id=booking, is_deleted=False):
        chat_changed(message.sender_id_id, message.receiver_id_id, message.pk)
