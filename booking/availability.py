"""Pending listings stay available; passed schedules are only a display warning."""
from zoneinfo import ZoneInfo
from django.db.models import Exists, OuterRef
from django.utils import timezone
from .models import tbl_booking, tbl_booking_assignment

UNAVAILABLE_MESSAGE = 'This post is no longer available as it has already been booked or cancelled.'
PASSED_SCHEDULE_MESSAGE = 'Cannot accept: Original date has passed. Please send a new offer in chat with an updated date.'
PH_TIME = ZoneInfo('Asia/Manila')


def with_post_state(queryset):
    return queryset.annotate(
        _has_assignment=Exists(tbl_booking_assignment.objects.filter(booking_id=OuterRef('pk'))),
    )


def available_public_posts(queryset=None):
    queryset = tbl_booking.objects.all() if queryset is None else queryset
    return with_post_state(queryset).filter(
        recipient_id__isnull=True, booking_status='Pending',
        poster_id__is_active=True, _has_assignment=False,
    )


def schedule_expired(booking):
    return booking.start_time.astimezone(PH_TIME).date() < timezone.localdate(timezone=PH_TIME)


def has_assignment(booking):
    return booking._has_assignment if hasattr(booking, '_has_assignment') else booking.assignments.exists()


def post_status(booking):
    status = booking.booking_status
    if status != 'Pending':
        return {'Accepted': 'Booked', 'InProgress': 'Ongoing'}.get(status, status)
    if has_assignment(booking):
        return 'Booked'
    if not booking.poster_id.is_active:
        return 'Unavailable'
    return 'Available'
