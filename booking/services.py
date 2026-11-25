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
SHORT_TERM_MAX_DAYS = 7


def validate_booking_duration(booking_type, start_time, end_time):
    if not start_time or not end_time:
        return
    days = (end_time.astimezone(PH_TIME).date() - start_time.astimezone(PH_TIME).date()).days
    if booking_type == 'short_term' and days > SHORT_TERM_MAX_DAYS:
        raise ValidationError({
            'end_time': 'The end date for a short-term booking can be up to 7 days after the start date.'
        })
    if booking_type == 'long_term' and days < 30:
        raise ValidationError({'end_time': 'SerbiSure requires at least 30 days for long-term posts. Choose a later end date.'})


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


def _participant_display_name(user):
    full = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip()
    return full or getattr(user, 'username', '') or 'Service Partner'


def get_pending_mandatory_review(user):
    """Return the most recent reviewable Completed booking still missing a review from user."""
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    if getattr(user, 'account_type', None) not in ('Homeowner', 'Kasambahay'):
        return None
    from reviews.models import tbl_review
    reviewed_ids = tbl_review.objects.filter(reviewer_id=user).values_list('booking_id_id', flat=True)
    completed = (
        tbl_booking.objects.filter(booking_status='Completed')
        .filter(Q(poster_id=user) | Q(assignments__accepter_id=user))
        .exclude(pk__in=reviewed_ids)
        .select_related('poster_id')
        .prefetch_related('assignments__accepter_id')
        .distinct()
        .order_by('-completed_at', '-createdAt')
    )
    for booking in completed:
        assignment = next(iter(booking.assignments.all()), None)
        if not assignment or not assignment.accepter_id_id:
            continue
        if booking.poster_id_id == assignment.accepter_id_id:
            continue
        reviewee = assignment.accepter_id if booking.poster_id_id == user.pk else booking.poster_id
        if not reviewee or reviewee.pk == user.pk:
            continue
        return {
            'booking_id': str(booking.pk),
            'reviewee_name': _participant_display_name(reviewee),
            'service_category': booking.service_category if isinstance(booking.service_category, list) else [],
        }
    return None


def assert_no_pending_review(user):
    pending = get_pending_mandatory_review(user)
    if pending:
        raise PermissionDenied({
            'code': 'pending_mandatory_review',
            'detail': (
                f"Please submit your review for your completed booking with {pending['reviewee_name']} "
                "before creating, applying for, or accepting a new booking."
            ),
            'pending_review': pending,
        })


def ensure_eligible_pair(poster, accepter):
    if poster.pk == accepter.pk:
        raise ValidationError({'error': 'You cannot accept or propose on your own booking.'})
    if {poster.account_type, accepter.account_type} != {'Homeowner', 'Kasambahay'}:
        raise PermissionDenied('An engagement requires one Homeowner and one Kasambahay.')
    for person in (poster, accepter):
        assert_booking_unrestricted(person)
        if not person.is_active or person.verification_status != 'Verified':
            raise PermissionDenied('Both participants must be active, unrestricted and verified.')
        from accounts.workforce import REQUIRED_DOCUMENTS
        required = REQUIRED_DOCUMENTS[person.account_type]
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
    validate_booking_duration(booking.booking_type, booking.start_time, booking.end_time)
    if booking.rate_needs_review and not (booking.poster_id.account_type == 'Kasambahay' and not booking.recipient_id_id):
        raise ValidationError({'error': 'The poster must confirm the monthly amount on this legacy post before it can be accepted.'})


def is_public_ad(booking):
    """Hours-only advertisements have no agreed calendar date until a hire."""
    return (not booking.recipient_id_id and not booking.work_days and booking.end_time is None
            and booking.daily_start_time is not None and booking.daily_end_time is not None)


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


def validate_hire_schedule(booking_type, schedule):
    """Validate optional calendar schedule supplied when hiring an applicant on a public post."""
    start_time = schedule.get('start_time')
    end_time = schedule.get('end_time')
    daily_start = schedule.get('daily_start_time')
    daily_end = schedule.get('daily_end_time')
    days = list(schedule.get('work_days') or [])
    if not days or len(set(days)) != len(days):
        raise ValidationError({'work_days': 'Select at least one unique working day.'})
    if (daily_start is None) != (daily_end is None) or not daily_start or not daily_end:
        raise ValidationError({'daily_end_time': 'Provide both daily working times.'})
    if daily_start >= daily_end:
        raise ValidationError({'daily_end_time': 'The end of the shift must be after its start.'})
    now = timezone.now()
    if not start_time or start_time <= now:
        raise ValidationError({'start_time': 'Start time must be in the future.'})
    if end_time is not None:
        if end_time <= now:
            raise ValidationError({'end_time': 'End time must be in the future.'})
        if end_time <= start_time:
            raise ValidationError({'end_time': 'End time must be strictly after the start time.'})
    for field, stamp, clock in (('start_time', start_time, daily_start), ('end_time', end_time, daily_end)):
        if stamp and clock and stamp.astimezone(PH_TIME).time().replace(tzinfo=None) != clock:
            raise ValidationError({field: 'The schedule must match the selected working time in the Philippines.'})
    weekdays = ['M', 'T', 'W', 'Th', 'F', 'S', 'Su']
    start_local = start_time.astimezone(PH_TIME)
    start_day = weekdays[start_local.weekday()]
    short_range = (booking_type == 'short_term' and end_time is not None and end_time.astimezone(PH_TIME).date() > start_local.date())
    if short_range:
        day_span = (end_time.astimezone(PH_TIME).date() - start_local.date()).days
        first = start_local.weekday()
        if not any(weekdays[(first + offset) % 7] in days for offset in range(min(day_span + 1, 7))):
            raise ValidationError({'work_days': 'Select a working day within the date range.'})
    elif start_day not in days:
        raise ValidationError({'work_days': 'The start date must fall on a selected working day.'})
    validate_booking_duration(booking_type, start_time, end_time)


def enforce_pair_cap(booking, accepter, *, service_start=None):
    at = service_start if service_start is not None else (timezone.now() if is_public_ad(booking) else booking.start_time)
    if booking.booking_type == 'short_term' and pair_booking_count(booking.poster_id_id, accepter.pk, at) >= SHORT_TERM_CAP:
        raise ValidationError({'code': 'short_term_monthly_cap',
                               'error': 'This pair has reached 3 Part-time / Daily bookings this month. Switch to a Full-time / Monthly arrangement instead (RA 10361).'})


def lock_participants(poster_id, accepter_id):
    # Stable user locks serialize the pair across different listings, not just one post.
    users = list(get_user_model().objects.select_for_update(no_key=True).filter(pk__in=[poster_id, accepter_id]).order_by('pk'))
    return {person.pk: person for person in users}


@transaction.atomic
def accept_engagement(booking_id, actor, proposal_id=None, *, schedule=None):
    preview = get_object_or_404(tbl_booking, pk=booking_id)
    proposal = get_object_or_404(tbl_booking_proposal, pk=proposal_id, booking_id=preview) if proposal_id else None
    if proposal and preview.poster_id_id != actor.pk:
        raise PermissionDenied('Only the poster can accept this proposal.')
    accepter_id = proposal.proposer_id_id if proposal else actor.pk
    people = lock_participants(preview.poster_id_id, accepter_id)
    booking = get_object_or_404(tbl_booking.objects.select_for_update(), pk=booking_id)
    poster, accepter = people[booking.poster_id_id], people[accepter_id]
    ensure_eligible_pair(poster, accepter)
    assert_no_pending_review(people[actor.pk])
    if booking.recipient_id_id and booking.recipient_id_id != accepter.pk:
        raise PermissionDenied('This private offer is addressed to another participant.')
    public_ad = is_public_ad(booking)
    ensure_open(booking, require_future=not (proposal and public_ad))
    if proposal:
        proposal = tbl_booking_proposal.objects.select_for_update().get(pk=proposal.pk)
        if proposal.status != 'Pending':
            raise ValidationError({'error': 'This proposal has already been resolved.'})
    if schedule is not None:
        validate_hire_schedule(booking.booking_type, schedule)
    amount = proposal.proposed_rate if proposal else booking.rate_amount
    validate_rate(amount, booking.booking_type, booking.region, booking.full_address, period=booking.rate_period)
    enforce_pair_cap(booking, accepter, service_start=schedule['start_time'] if schedule is not None else None)
    booking.rate_amount = amount
    booking.rate_needs_review = False
    booking.booking_status = 'Accepted'
    fields = ['rate_amount', 'rate_needs_review', 'booking_status']
    if schedule is not None:
        booking.start_time = schedule['start_time']
        booking.end_time = schedule.get('end_time')
        booking.daily_start_time = schedule['daily_start_time']
        booking.daily_end_time = schedule['daily_end_time']
        booking.work_days = list(schedule['work_days'])
        fields.extend(['start_time', 'end_time', 'daily_start_time', 'daily_end_time', 'work_days'])
    elif proposal and public_ad:
        booking.start_time = timezone.now()
        fields.append('start_time')
    booking.save(update_fields=fields)
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
    assert_no_pending_review(people[actor.pk])
    if booking.recipient_id_id and booking.recipient_id_id != actor.pk:
        raise PermissionDenied('This private offer is addressed to another participant.')
    enforce_pair_cap(booking, actor)
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
    if booking.proposals.filter(proposer_id=actor, status='Pending').exists():
        raise ValidationError({'error': 'You already have a pending proposal for this post.'})
    return tbl_booking_proposal.objects.create(booking_id=booking, proposer_id=actor,
                                                proposed_rate=amount, message=message), True

