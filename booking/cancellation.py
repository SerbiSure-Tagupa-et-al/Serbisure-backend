"""One cancellation policy for both roles and both engagement types."""
from datetime import timedelta
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from accounts.penalties import penalty_status, refresh_monthly_penalties
from .models import tbl_booking
from .services import lock_participants

SAFETY_REASON = 'Safety / Inappropriate Homeowner Behavior'
HOMEOWNER_REASONS = ['Change of household plans', 'Emergency', 'Worker unresponsive', 'Other']
WORKER_REASONS = ['Sickness / Medical', 'Family emergency', 'Transportation / Weather', SAFETY_REASON, 'Other']


def cancellation_reasons(user):
    return WORKER_REASONS if user.account_type == 'Kasambahay' else HOMEOWNER_REASONS


def cancellation_points(booking, at=None):
    if booking.booking_status == 'Pending':
        return 0
    remaining = booking.start_time - (at or timezone.now())
    return 2 if remaining <= timedelta(0) else 1 if remaining < timedelta(hours=24) else 0


def assert_cancellation_access(booking, actor):
    assignment = booking.assignments.first()
    if actor.pk not in (booking.poster_id_id, assignment.accepter_id_id if assignment else None,
                         booking.recipient_id_id if booking.booking_status == 'Pending' else None,
                         booking.cancelled_by_id if booking.booking_status == 'Cancelled' else None):
        raise PermissionDenied('You cannot cancel this booking.')
    if booking.booking_status == 'Completed':
        raise ValidationError({'detail': 'Completed bookings cannot be cancelled.'})
    return assignment


def cancellation_preview(booking, actor):
    assert_cancellation_access(booking, actor)
    if booking.booking_status == 'Cancelled':
        raise ValidationError({'detail': 'This booking is already cancelled.'})
    return {**penalty_status(actor), 'points_to_add': cancellation_points(booking),
            'reasons': cancellation_reasons(actor)}


@transaction.atomic
def cancel_booking(booking_id, actor, reason, expected_points=None, *, pending_only=False):
    preview = get_object_or_404(tbl_booking, pk=booking_id)
    people = lock_participants(preview.poster_id_id, actor.pk)
    actor = people[actor.pk]
    booking = get_object_or_404(tbl_booking.objects.select_for_update(), pk=booking_id)
    assignment = assert_cancellation_access(booking, actor)
    if booking.booking_status == 'Cancelled':
        if booking.cancelled_by_id == actor.pk:
            return booking, actor, False
        raise ValidationError({'detail': 'This booking is already cancelled.'})
    if pending_only and (booking.booking_status != 'Pending' or booking.recipient_id_id or actor.pk != booking.poster_id_id):
        raise ValidationError({'detail': 'This post has already been booked. Refresh your posts to see its current status.'})
    if reason not in cancellation_reasons(actor):
        raise ValidationError({'reason': 'Choose a cancellation reason.'})
    refresh_monthly_penalties(actor)
    points = cancellation_points(booking)
    if expected_points is not None and expected_points != points:
        raise ValidationError({'detail': 'Cancellation timing changed. Review the penalty and try again.'})
    actor.penalty_points = min(3, max(0, actor.penalty_points) + points)
    if actor.penalty_points >= 3:
        actor.is_restricted = True
    actor.save(update_fields=['penalty_points', 'is_restricted'])
    booking.booking_status = 'Cancelled'
    booking.cancellation_reason = reason
    booking.cancelled_by = actor
    booking.cancelled_at = timezone.now()
    booking.cancellation_penalty_points = points
    booking.save(update_fields=['booking_status', 'cancellation_reason', 'cancelled_by', 'cancelled_at', 'cancellation_penalty_points'])
    booking.proposals.filter(status='Pending').update(status='Rejected')
    if assignment:
        from notifications.services import send_in_app_notification
        partner = assignment.accepter_id if actor.pk == booking.poster_id_id else booking.poster_id
        send_in_app_notification(receiver=partner, sender=actor, message='Your booking was cancelled. Open your booking history for details.')
    return booking, actor, True
