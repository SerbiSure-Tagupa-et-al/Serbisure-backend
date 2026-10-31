"""Read-only village pass presentation. No arrival or booking mutations."""
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from accounts.workforce import PH_TIME
from core.utils import get_signed_cloudinary_url, normalize_ph_phone_number

DAY_CODES = ('M', 'T', 'W', 'Th', 'F', 'S', 'Su')
DAY_NAMES = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
GATE_PASS_SALT = 'booking.gate_pass.v1'
GATE_PASS_MAX_AGE = 86400


def generate_gate_pass_token(booking_id):
    return signing.dumps(str(booking_id), salt=GATE_PASS_SALT)


def gate_pass_url(request, booking_id):
    query = urlencode({'token': generate_gate_pass_token(booking_id)})
    url = request.build_absolute_uri(f"{reverse('booking-public-pass')}?{query}")
    # Hosted passes use HTTPS even if the application sees an internal HTTP hop.
    if not settings.DEBUG and url.startswith('http://'):
        url = 'https://' + url[len('http://'):]
    return url

# Presentation states only. Booking statuses and entry rules remain unchanged.
PASS_WORDING = {
    'allowed': ('success', 'check', 'OK to enter', 'Booking confirmed. Check face and ID.'),
    'wrong_time': ('warning', 'clock', 'Do not enter yet', 'Real booking. Outside work hours.'),
    'pending': ('warning', 'hourglass', 'Do not enter yet', 'This booking is not confirmed yet.'),
    'cancelled': ('danger', 'close', 'Do not enter', 'This booking was cancelled.'),
    'ended': ('danger', 'calendar-end', 'Do not enter', 'This booking has ended.'),
    'identity_check': ('warning', 'id-card', 'Do not enter yet', 'Call the homeowner to check the IDs.'),
    'incomplete': ('warning', 'info-square', 'Do not enter yet', 'Call the homeowner. Details are missing.'),
    'not_found': ('danger', 'search-x', 'Do not enter', 'This code is not in SerbiSure.'),
    'unavailable': ('warning', 'warning', 'Do not enter yet', 'We could not check. Please try again.'),
    'rate_limited': ('warning', 'warning', 'Do not enter yet', 'Please wait a minute, then try again.'),
    'token_expired': ('danger', 'calendar-end', 'Do not enter', 'This pass is over 24 hours old. Open SerbiSure online to get a new pass.'),
}


def pass_status(state):
    color, icon, title, message = PASS_WORDING[state]
    return dict(pass_state=state, status_class=color, status_icon=icon,
                title=title, message=message, entry_allowed=state == 'allowed')


def clock_label(value):
    return value.strftime('%I:%M %p').lstrip('0')


def date_label(value):
    return f'{value:%b} {value.day}, {value.year}'


def next_entry_label(start, end, now, daily_start, daily_end, work_days, last_day):
    """Display the next saved work window; this never grants entry or edits dates."""
    first = max(now.date(), start.date())
    for offset in range(8):
        day = first + timedelta(days=offset)
        if last_day and day > last_day:
            break
        working = DAY_CODES[day.weekday()] in work_days if work_days else start.date() == last_day == day
        if not working:
            continue
        opens = max(datetime.combine(day, daily_start, PH_TIME), start)
        closes = datetime.combine(day, daily_end, PH_TIME)
        if end:
            closes = min(closes, end)
        if opens < closes and now < closes:
            label = 'Today' if day == now.date() else f'{day:%a}, {day:%b} {day.day}'
            if day.year != now.year:
                label += f', {day.year}'
            return f'Valid: {label} · {clock_label(opens)} – {clock_label(closes)}'
    return 'No work time left. Call the homeowner.'


def person_details(person):
    return {
        'name': ' '.join(part.strip() for part in (person.first_name, person.middle_name, person.last_name) if part and part.strip()) or 'Name not provided',
        'photo': get_signed_cloudinary_url(person.profile_link, width=480, height=480),
        'verified': person.is_active and person.verification_status == 'Verified',
    }


def pass_details(booking, now=None):
    now = (now or timezone.now()).astimezone(PH_TIME)
    start = booking.start_time.astimezone(PH_TIME)
    end = booking.end_time.astimezone(PH_TIME) if booking.end_time else None
    expired = bool(end and now >= end) or (
        booking.booking_type == 'short_term' and not end and now.date() > start.date()
    )
    context = {
        'booking_code': f'BK-{str(booking.pk)[:8].upper()}',
        'checked_at': f"{date_label(now)}, {now.strftime('%I:%M:%S %p').lstrip('0')}",
        **pass_status('ended'),
        'service': ', '.join(dict(booking.SERVICES_OFFER_CHOICES).get(value, value) for value in booking.service_category),
    }
    if booking.booking_status in ('Cancelled', 'Completed') or expired:
        if booking.booking_status == 'Cancelled':
            context.update(pass_status('cancelled'))
        return context
    if booking.booking_status == 'Pending':
        context.update(pass_status('pending'))
        return context

    assignment = next(iter(booking.assignments.all()), None)
    if booking.booking_status not in ('Accepted', 'InProgress') or assignment is None:
        context.update(pass_status('identity_check'))
        context['message'] = 'Call the homeowner. No worker is booked.'
        return context
    participants = {person.account_type: person for person in (booking.poster_id, assignment.accepter_id)}
    if set(participants) != {'Kasambahay', 'Homeowner'} or not all(person.is_active for person in participants.values()):
        context.update(pass_status('identity_check'))
        context['message'] = 'Call the homeowner to check this booking.'
        return context

    worker = person_details(participants['Kasambahay'])
    owner = person_details(participants['Homeowner'])
    phone = normalize_ph_phone_number(participants['Homeowner'].contact_number)
    first_day = start.date()
    last_day = end.date() if end else (first_day if booking.booking_type == 'short_term' else None)
    daily_start, daily_end = booking.daily_start_time, booking.daily_end_time
    # Legacy single-day bookings can use their actual saved start/end times.
    # Never invent daily hours or working days for an incomplete multi-day record.
    if not daily_start and end and first_day == last_day:
        daily_start = start.time()
    if not daily_end and end and first_day == last_day:
        daily_end = end.time()
    hours_known = bool(daily_start and daily_end and daily_end > daily_start)
    within_dates = first_day <= now.date() and (last_day is None or now.date() <= last_day)
    working_today = DAY_CODES[now.weekday()] in booking.work_days if booking.work_days else first_day == last_day == now.date()
    in_hours = False
    if hours_known:
        opens = max(datetime.combine(now.date(), daily_start, PH_TIME), start)
        closes = datetime.combine(now.date(), daily_end, PH_TIME)
        if end:
            closes = min(closes, end)
        in_hours = opens <= now < closes

    context.update(
        **pass_status('wrong_time'),
        worker=worker, homeowner=owner, phone=phone, address=booking.full_address or 'Address not provided',
        schedule_dates=('Today' if first_day == now.date() else date_label(start)) + (
            f' – {date_label(end)}' if end and first_day != last_day else
            ' onwards (no end date)' if last_day is None else ''),
        hours=f'{clock_label(daily_start)} – {clock_label(daily_end)}' if hours_known else 'Ask the homeowner',
        work_days=', '.join(label for code, label in zip(DAY_CODES, DAY_NAMES) if code in booking.work_days) or 'Ask the homeowner',
    )
    if not worker['verified'] or not owner['verified']:
        context.update(pass_status('identity_check'))
    elif not booking.full_address or not hours_known or (not booking.work_days and first_day != last_day):
        context.update(pass_status('incomplete'))
    elif within_dates and working_today and in_hours:
        context.update(pass_status('allowed'), valid_today=date_label(now))
    else:
        if not within_dates:
            context['message'] = 'Real booking. It is for another day.'
        elif not working_today:
            context['message'] = 'Real booking. Today is not a work day.'
        context['entry_when'] = next_entry_label(start, end, now, daily_start, daily_end, booking.work_days, last_day)
    return context
