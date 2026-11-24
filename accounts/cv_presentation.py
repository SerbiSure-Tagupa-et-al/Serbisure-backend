"""Pure, privacy-filtered CV formatting shared by the mobile preview and PDF."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from django.utils import timezone

DAY_TOKENS = ['M', 'T', 'W', 'Th', 'F', 'S', 'Su']
DAY_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']


def clean(value):
    return re.sub(r'\s+', ' ', str(value)).strip() if value is not None else ''


def unique(items):
    result, seen = [], set()
    for item in items or []:
        text = clean(item)
        if text and text.casefold() not in seen:
            result.append(text)
            seen.add(text.casefold())
    return result


def days(values):
    indices = sorted({DAY_TOKENS.index(value) for value in values or [] if value in DAY_TOKENS})
    if indices == list(range(7)):
        return 'Every day'
    if indices == list(range(5)):
        return 'Weekdays'
    if indices == [5, 6]:
        return 'Weekends'
    runs = []
    index = 0
    while index < len(indices):
        end = index
        while end + 1 < len(indices) and indices[end + 1] == indices[end] + 1:
            end += 1
        if end - index >= 2:
            runs.append(f'{DAY_NAMES[indices[index]]}-{DAY_NAMES[indices[end]]}')
        else:
            runs.extend(DAY_NAMES[i] for i in indices[index:end + 1])
        index = end + 1
    return ', '.join(runs)


def shifts(values):
    return ', '.join(value.title() for value in ['morning', 'afternoon', 'night'] if value in (values or []))


def money(value, unit):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            return ''
        number = f'{amount:,.2f}'
        return f"PHP {number[:-3] if number.endswith('.00') else number} / {unit}"
    except (InvalidOperation, ValueError):
        return ''


def available(value, today=None):
    if not value:
        return ''
    try:
        target = date.fromisoformat(str(value))
    except ValueError:
        return ''
    today = today or timezone.localdate(timezone=timezone.get_fixed_timezone(480))
    return 'Available now' if target <= today else f'{MONTHS[target.month - 1]} {target.day}, {target.year}'


def phone(value):
    text = clean(value)
    digits = re.sub(r'[\s()-]', '', text)
    if re.fullmatch(r'(?:\+63|0)9\d{9}', digits):
        local = digits[-10:]
        return f'+63 {local[:3]} {local[3:6]} {local[6:]}'
    return text


def build(data, labels, today=None):
    d = data['details']
    label = lambda value: labels.get(clean(value), clean(value))
    facts, sections = [], []
    def fact(title, value):
        if value:
            facts.append({'label': title, 'value': value})
    def section(title, kind, items):
        if items:
            sections.append({'title': title, 'kind': kind, 'items': items})
    def pairs(values):
        return [{'label': key, 'value': clean(value)} for key, value in values if clean(value)]
    rates = [money(d.get('desired_daily_rate'), 'day'), money(d.get('desired_monthly_rate'), 'month')]
    fact('Expected rate', '\n'.join(value for value in rates if value))
    fact('Available from', available(d.get('available_from'), today))
    fact('Languages', ', '.join(unique((data.get('languages') or '').split(','))))
    fact('Work setup', label(d.get('work_setup')))
    section('About', 'text', [clean(d['experience_summary'])] if clean(d.get('experience_summary')) else [])
    section('Practical skills', 'chips', unique(d.get('practical_skills')))
    section('Availability & preferences', 'pairs', pairs([
        ('Working days', days(d.get('available_days'))), ('Preferred shifts', shifts(d.get('preferred_shifts'))),
        ('Work areas', ', '.join(unique(d.get('preferred_work_areas')))), ('Engagement', label(d.get('engagement_preference'))),
    ]))
    experience = [label(d.get('experience_level'))] if d.get('experience_level') else []
    if d.get('experience_level') != 'new' and d.get('experience_duration'):
        experience.append(label(d['experience_duration']))
    section('Experience', 'text', experience)
    training = []
    for item in d.get('training') or []:
        if clean(item.get('name')):
            training.append(' · '.join(clean(value) for value in [item['name'], item.get('provider'), item.get('year')] if clean(value)))
    section('Training', 'text', training)
    # Presence already reflects the worker's sharing choice. Zero children is real data.
    children = d.get('number_of_children')
    section('Background', 'pairs', pairs([
        ('Education', d.get('education')), ('Civil status', d.get('civil_status')),
        ('Number of children', str(children) if children is not None else ''),
    ]))
    trust = []
    if d.get('references_available'):
        trust.append('Work reference available if asked.')
    status = clean(data.get('verification_status'))
    if status and status != 'Verified':
        trust.append(f'Verification: {status}')
    perf = data['performance']
    stats = []
    if perf['completed_jobs']:
        stats.append(f"{perf['completed_jobs']} completed jobs")
    if perf.get('recommended_count'):
        rec = perf['recommended_count']
        stats.append(f"Recommended by {rec} {'Homeowner' if rec == 1 else 'Homeowners'}")
    if perf['review_count']:
        stats.append(f"{perf['review_count']} reviews")
        if perf['average_rating'] is not None:
            stats.append(f"{perf['average_rating']}/5 stars")
        if perf['positive_percentage'] is not None:
            stats.append(f"{perf['positive_percentage']}% positive feedback")
    if stats:
        trust.append(' · '.join(stats))
    verified_lines = []
    for job in data.get('work_history') or []:
        services = ', '.join(job.get('service_labels') or []) or 'Household Service'
        term = 'Full-time' if job.get('booking_type') == 'long_term' else 'Part-time'
        parts = [services, term]
        if job.get('area'):
            parts.append(job['area'])
        if job.get('would_recommend'):
            parts.append('Recommended by Homeowner')
        verified_lines.append(' · '.join(parts))
    section('Verified SerbiSure Work History', 'text', verified_lines)
    return {'name': clean(data['name']), 'services': ', '.join(label(value) for value in unique(d.get('service_categories'))),
            'location': clean(data.get('location')), 'phone': phone(data.get('contact_number')),
            'verified': status == 'Verified', 'facts': facts, 'sections': sections, 'trust': trust,
            'notice': data['notice'], 'generated_date': data['generated_at'][:10]}
