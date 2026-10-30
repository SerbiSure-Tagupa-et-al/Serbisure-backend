"""Authoritative engagement transitions shared by direct and proposal acceptance."""
from datetime import datetime
from zoneinfo import ZoneInfo
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from .models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal
from .wage_policy import validate_rate
from accounts.penalties import assert_booking_unrestricted
from .availability import UNAVAILABLE_MESSAGE, PASSED_SCHEDULE_MESSAGE

PH_TIME = ZoneInfo('Asia/Manila')
SHORT_TERM_CAP = 3


def month_bounds(at=None):
    current = (at or timezone.now()).astimezone(PH_TIME)
    start = datetime(current.year, current.month, 1, tzinfo=PH_TIME)
    end = datetime(current.year + (current.month == 12), current.month % 12 + 1, 1, tzinfo=PH_TIME)
    return start, end


def pair_booking_count(first_id, second_id, at=None):
    start, end = month_bounds(at)
    # Count the Philippine service-start month, including advance reservations.
    # Accepted cancellations consume a slot: cancelling must not evade the cap.
    return tbl_booking_assignment.objects.filter(
        Q(booking_id__poster_id_id=first_id, accepter_id_id=second_id) |
        Q(booking_id__poster_id_id=second_id, accepter_id_id=first_id),
        booking_id__booking_type='short_term', booking_id__start_time__gte=start, booking_id__start_time__lt=end,
    ).values('booking_id').distinct().count()


def ensure_eligible_pair(poster, accepter):
    if poster.pk == accepter.pk:
        raise ValidationError({'error': 'You cannot accept or propose on your own booking.'})
    if {poster.account_type, accepter.account_type} != {'Homeowner', 'Kasambahay'}:
        raise PermissionDenied('An engagement requires one Homeowner and one Kasambahay.')
    for person in (poster, accepter):
        assert_booking_unrestricted(person)
        if not person.is_active or person.verification_status != 'Verified':
            raise PermissionDenied('Both participants must be active, unrestricted and verified.')
        required = ('national_id_front', 'national_id_back') if person.account_type == 'Homeowner' else ('nbi_clearance', 'police_clearance')
        from verifications.models import tbl_documents
        valid_types = set(tbl_documents.objects.filter(
            user_profile=person, verification_status='Verified', document_type__in=required,
        ).filter(Q(valid_until__isnull=True) | Q(valid_until__gte=timezone.localdate(timezone=PH_TIME))).values_list('document_type', flat=True))
        if not set(required).issubset(valid_types):
            raise PermissionDenied('Both participants need current approved identity documents.')


def ensure_open(booking, *, require_future=True):
    if booking.booking_status != 'Pending' or booking.assignments.exists():
        raise ValidationError({'code': 'post_unavailable', 'error': UNAVAILABLE_MESSAGE})
    if require_future and booking.start_time <= timezone.now():
        raise ValidationError({'code': 'schedule_passed', 'error': PASSED_SCHEDULE_MESSAGE})
    if (booking.booking_type == 'long_term' and booking.end_time
            and (booking.end_time.astimezone(PH_TIME).date() - booking.start_time.astimezone(PH_TIME).date()).days < 30):
        raise ValidationError({'end_time': 'SerbiSure requires at least 30 days for long-term posts. Choose a later end date.'})
    if booking.rate_needs_review:
        raise ValidationError({'error': 'The poster must confirm the monthly amount on this legacy post before it can be accepted.'})


def close_other_applications(booking, selected_user):
    from notifications.services import send_in_app_notification
    selected = booking.proposals.filter(proposer_id=selected_user, status='Pending')
    selected.update(status='Accepted')
    others = booking.proposals.filter(status='Pending').exclude(proposer_id=selected_user)
    recipients = {row.proposer_id_id: row.proposer_id for row in others.select_related('proposer_id')}
    others.update(status='Rejected')
    for person in recipients.values():
        send_in_app_notification(receiver=person, sender=booking.poster_id,
            message='A post you applied to has been booked. Your application is closed / not selected.')
    from realtime.events import publish
    publish([booking.poster_id_id, selected_user.pk, *recipients], {'type': 'booking.changed', 'booking_id': str(booking.pk)})


def enforce_pair_cap(booking, accepter):
    if booking.booking_type == 'short_term' and pair_booking_count(booking.poster_id_id, accepter.pk, booking.start_time) >= SHORT_TERM_CAP:
        raise ValidationError({'code': 'short_term_monthly_cap',
                               'error': 'This pair has reached three short-term bookings this month. Use a long-term engagement and the DOLE contract form to continue.'})


def lock_participants(poster_id, accepter_id):
    # Stable user locks serialize the pair across different listings, not just one post.
    users = list(get_user_model().objects.select_for_update(no_key=True).filter(pk__in=[poster_id, accepter_id]).order_by('pk'))
    return {person.pk: person for person in users}


@transaction.atomic
def accept_engagement(booking_id, actor, proposal_id=None):
    preview = get_object_or_404(tbl_booking, pk=booking_id)
    proposal = get_object_or_404(tbl_booking_proposal, pk=proposal_id, booking_id=preview) if proposal_id else None
    if proposal and preview.poster_id_id != actor.pk:
        raise PermissionDenied('Only the poster can accept this proposal.')
    accepter_id = proposal.proposer_id_id if proposal else actor.pk
    people = lock_participants(preview.poster_id_id, accepter_id)
    booking = get_object_or_404(tbl_booking.objects.select_for_update(), pk=booking_id)
    poster, accepter = people[booking.poster_id_id], people[accepter_id]
    ensure_eligible_pair(poster, accepter)
    if booking.recipient_id_id and booking.recipient_id_id != accepter.pk:
        raise PermissionDenied('This private offer is addressed to another participant.')
    ensure_open(booking)
    if proposal:
        proposal = tbl_booking_proposal.objects.select_for_update().get(pk=proposal.pk)
        if proposal.status != 'Pending':
            raise ValidationError({'error': 'This proposal has already been resolved.'})
    amount = proposal.proposed_rate if proposal else booking.rate_amount
    validate_rate(amount, booking.booking_type, booking.region, booking.full_address, period=booking.rate_period)
    enforce_pair_cap(booking, accepter)
    booking.rate_amount = amount
    booking.booking_status = 'Accepted'
    booking.save(update_fields=['rate_amount', 'booking_status'])
    tbl_booking_assignment.objects.create(booking_id=booking, accepter_id=accepter)
    if proposal:
        proposal.status = 'Accepted'
        proposal.save(update_fields=['status'])
    close_other_applications(booking, accepter)
    return booking, accepter


@transaction.atomic
def create_proposal(booking_id, actor, amount, period, message, *, apply_at_posted_rate=False):
    preview = get_object_or_404(tbl_booking, pk=booking_id)
    people = lock_participants(preview.poster_id_id, actor.pk)
    from chat.moderation import inspect_outgoing
    message = inspect_outgoing(people[actor.pk], people[preview.poster_id_id], message, channel='proposal')
    booking = get_object_or_404(tbl_booking.objects.select_for_update(), pk=booking_id)
    ensure_eligible_pair(people[booking.poster_id_id], people[actor.pk])
    if booking.recipient_id_id and booking.recipient_id_id != actor.pk:
        raise PermissionDenied('This private offer is addressed to another participant.')
    if apply_at_posted_rate:
        if booking.poster_id.account_type != 'Homeowner' or booking.recipient_id_id:
            raise ValidationError({'error': 'Applications are only available on public job posts.'})
        ensure_open(booking, require_future=False)
        existing = booking.proposals.filter(proposer_id=actor, status__in=['Pending', 'Accepted']).order_by('-createdAt').first()
        if existing:
            return existing, False
        amount, period = booking.rate_amount, booking.rate_period
    ensure_open(booking, require_future=False)
    amount = validate_rate(amount, booking.booking_type, booking.region, booking.full_address,
                           field='proposed_rate', period=period)
    enforce_pair_cap(booking, actor)
    if booking.proposals.filter(proposer_id=actor, status='Pending').exists():
        raise ValidationError({'error': 'You already have a pending proposal for this post.'})
    return tbl_booking_proposal.objects.create(booking_id=booking, proposer_id=actor,
                                                proposed_rate=amount, message=message), True

