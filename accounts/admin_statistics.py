"""Database-only workforce and barangay coverage snapshots for the portal."""
import re
from collections import defaultdict
from rest_framework.exceptions import PermissionDenied
from .models import tbl_user_profile
from .workforce import credential_snapshot, SERVICES


def barangay_name(value):
    return re.sub(r'^(brgy\.?|barangay)\s+', '', (value or '').strip(), flags=re.I).strip()


def scope_for(request):
    if request.user.account_type == 'Barangay':
        if not barangay_name(request.user.barangay):
            raise PermissionDenied('Your LGU account has no assigned barangay.')
        return request.user.barangay
    return request.query_params.get('barangay') or 'All Barangays'


def scoped_residents(request):
    """The portal list and counters share active residents and normalized scope."""
    scope = scope_for(request)
    users = tbl_user_profile.objects.filter(
        account_type__in=['Kasambahay', 'Homeowner'], is_active=True,
    ).prefetch_related('documents')
    role = request.query_params.get('role', 'ALL')
    if role.upper() not in ('ALL', 'ALL ROLES'):
        users = users.filter(account_type__iexact=role)
    return [user for user in users if matches_scope(user.barangay, scope)]


def resident_snapshot(scope='All Barangays', restrict_directory=False):
    users = list(tbl_user_profile.objects.filter(account_type__in=['Kasambahay', 'Homeowner'], is_active=True).prefetch_related('documents').select_related('worker_profile'))
    active = {}
    for value in tbl_user_profile.objects.filter(account_type='Barangay', is_active=True).values_list('barangay', flat=True):
        name = barangay_name(value)
        if name:
            active.setdefault(name.casefold(), name)
    groups, names = defaultdict(list), dict(active)
    for user in users:
        name = barangay_name(user.barangay)
        key = name.casefold()
        groups[key].append(user)
        if name and key != 'unassigned':
            names.setdefault(key, name)
    key = barangay_name(scope).casefold()
    if key in ['all', 'all barangays']:
        scoped = users
    elif key in ['unassigned', 'no lgu coverage', 'no_lgu']:
        # An empty LGU list means every resident lacks coverage, not an empty queryset.
        scoped = [user for user in users if barangay_name(user.barangay).casefold() not in active]
    else:
        scoped = groups.get(key, [])
    statuses = {user.pk: credential_snapshot(user)[0] for user in users}
    def stats(residents):
        workers = [user for user in residents if user.account_type == 'Kasambahay' and statuses[user.pk] == 'verified']
        employed = sum(user.is_on_job for user in workers)
        counts = {'pending': 0, 'verified': 0, 'rejected': 0, 'expired': 0, 'noDocuments': 0}
        for user in residents:
            counts[{'unverified': 'noDocuments'}.get(statuses[user.pk], statuses[user.pk])] += 1
        return {'totalRegistered': len(residents), 'totalWorkers': len(workers), 'employed': employed,
                'available': len(workers) - employed,
                'employmentRatio': round(employed / len(workers) * 100) if workers else 0, **counts}
    totals = stats(scoped)
    rows = [{'name': name, 'status': 'ACTIVE' if name_key in active else 'INACTIVE', **stats(groups[name_key])}
            for name_key, name in sorted(names.items(), key=lambda entry: entry[1].casefold())
            if not restrict_directory or name_key == key]
    pending_documents = sum(doc.verification_status == 'Pending' for user in scoped for doc in user.documents.all())
    verified_workers = [user for user in scoped if user.account_type == 'Kasambahay' and statuses[user.pk] == 'verified']
    skills = []
    for category in SERVICES:
        count = sum(category in getattr(getattr(user, 'worker_profile', None), 'service_categories', []) for user in verified_workers)
        skills.append({'category': category, 'count': count, 'percentage': round(count / len(verified_workers) * 100, 1) if verified_workers else 0})
    return {'metrics': {'totalWorkers': totals['totalWorkers'], 'totalEmployed': totals['employed'],
                        'totalAvailable': totals['available'], 'employmentRatio': totals['employmentRatio'],
                        'totalHomeowners': sum(user.account_type == 'Homeowner' for user in scoped),
                        'pendingVerifications': pending_documents}, 'barangays': rows,
            'skills': {'totalWorkers': len(verified_workers), 'categories': skills}}


def matches_scope(barangay, scope):
    key = barangay_name(scope).casefold()
    if key in ('all', 'all barangays'):
        return True
    if key in ('unassigned', 'no lgu coverage', 'no_lgu'):
        active = {barangay_name(value).casefold() for value in tbl_user_profile.objects.filter(account_type='Barangay', is_active=True).values_list('barangay', flat=True)}
        return barangay_name(barangay).casefold() not in active
    return barangay_name(barangay).casefold() == key
