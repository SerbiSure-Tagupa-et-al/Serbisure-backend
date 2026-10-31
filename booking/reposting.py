"""Reposting creates a fresh listing and never changes old applications or agreements."""
import hashlib
import json
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied, ValidationError
from accounts.penalties import assert_booking_unrestricted
from .availability import post_status, schedule_expired
from .models import tbl_booking


@transaction.atomic
def repost_public_post(post_id, actor, schedule, key):
    actor = get_user_model().objects.select_for_update().get(pk=actor.pk)
    assert_booking_unrestricted(actor)
    if not actor.is_active or actor.verification_status != 'Verified':
        raise PermissionDenied('Please complete verification before publishing a post.')
    source = get_object_or_404(tbl_booking.objects.select_for_update(), pk=post_id,
                              poster_id=actor, recipient_id__isnull=True)
    digest = hashlib.sha256(json.dumps({'action': 'repost', 'post_id': str(post_id), 'schedule': schedule},
                                      sort_keys=True, default=str).encode()).hexdigest()
    existing = tbl_booking.objects.filter(poster_id=actor, offer_request_key=key).first()
    if existing:
        if existing.offer_payload_hash != digest:
            raise ValidationError({'detail': 'This request key has already been used for different post details.'})
        return existing, False
    if post_status(source) != 'Available' or not schedule_expired(source):
        raise ValidationError({'detail': 'Only expired, unbooked public posts can be reposted.'})
    fields = ('booking_type', 'work_setup', 'service_category', 'region', 'province', 'city', 'barangay',
              'street', 'floor_number', 'zip_code', 'special_instruction', 'rate_amount', 'rate_period')
    payload = {field: getattr(source, field) for field in fields}
    payload.update(schedule)
    from .serializers import BookingSerializer
    serializer = BookingSerializer(data=payload)
    # Older posts can have no recorded living setup. Preserve that value;
    # this endpoint only accepts a new schedule, never changed post details.
    if source.work_setup is None:
        serializer.fields['work_setup'].allow_null = True
    serializer.is_valid(raise_exception=True)
    if source.booking_type == 'short_term' and not serializer.validated_data.get('end_time'):
        raise ValidationError({'end_time': 'Choose an end time for this short-term post.'})
    listing = serializer.save(poster_id=actor, offer_request_key=key, offer_payload_hash=digest)
    return listing, True
