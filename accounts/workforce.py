"""Shared document eligibility and event-driven workforce history (Philippine time)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from django.db import transaction
from django.db.models import Min, OuterRef, Subquery
from django.utils import timezone

PH_TIME = ZoneInfo('Asia/Manila')
REQUIRED_DOCUMENTS = {
    'Kasambahay': ('nbi_clearance', 'police_clearance'),
    'Homeowner': ('national_id_front', 'national_id_back'),
}
SERVICES = ('Cleaning', 'Cooking', 'Child_care', 'Caregiver', 'Laundry', 'All-around')


def latest_required_documents(user):
    required = REQUIRED_DOCUMENTS.get(user.account_type, ())
    latest = {}
    for doc in sorted(user.documents.all(), key=lambda doc: (doc.created_at, str(doc.pk))):
        if doc.document_type in required:
            latest[doc.document_type] = doc
    return list(latest.values())


def document_is_expired(doc, today=None):
    today = today or timezone.localdate(timezone=PH_TIME)
    return bool(doc.valid_until and doc.valid_until < today)


def credential_snapshot(user, today=None):
    """Use the latest submission of each required credential, never an old approval."""
    required = REQUIRED_DOCUMENTS.get(user.account_type, ())
    docs = latest_required_documents(user)
    expiries = [doc.valid_until for doc in docs if doc.valid_until]
    valid_until = min(expiries) if expiries else None
    complete = bool(required) and len(docs) == len(required)
    approved = complete and all(doc.verification_status == 'Verified' for doc in docs)
    today = today or timezone.localdate(timezone=PH_TIME)
    if any(doc.verification_status == 'Rejected' for doc in docs):
        status = 'rejected'
    elif any(document_is_expired(doc, today) for doc in docs):
        status = 'expired'
    elif approved:
        status = 'verified'
    elif any(doc.verification_status in ('Pending', 'Verified') for doc in docs):
        status = 'pending'
    else:
        status = 'unverified'
    return status, approved, valid_until


def record_worker_status(user_id, using='default'):
    from .models import tbl_user_profile, tbl_worker_status_history
    with transaction.atomic(using=using):
        user = tbl_user_profile.objects.using(using).select_for_update().filter(pk=user_id).first()
        if not user:
            return
        previous = tbl_worker_status_history.objects.using(using).filter(worker_id_snapshot=user_id).order_by('-changed_at', '-id').first()
        if user.account_type != 'Kasambahay' and previous is None:
            return
        _, approved, valid_until = credential_snapshot(user)
        snapshot = dict(status=user.is_on_job, barangay=user.barangay or '',
                        is_verified=approved and user.is_active and user.account_type == 'Kasambahay',
                        clearance_valid_until=valid_until)
        if previous is None or any(getattr(previous, key) != value for key, value in snapshot.items()):
            tbl_worker_status_history.objects.using(using).create(worker=user, worker_id_snapshot=user.pk, **snapshot)


def monthly_workforce(scope, live_metrics, matches_scope):
    from .models import tbl_worker_status_history as History
    now = timezone.now().astimezone(PH_TIME)
    history_since = History.objects.aggregate(first=Min('changed_at'))['first']
    points = []
    for offset in range(5, -1, -1):
        index = now.year * 12 + now.month - 1 - offset
        year, month0 = divmod(index, 12)
        next_index = index + 1
        end_year, end_month0 = divmod(next_index, 12)
        # Exclusive next-month boundary includes every subsecond of the last day.
        boundary = datetime(end_year, end_month0 + 1, 1, tzinfo=PH_TIME)
        point = {'month': datetime(year, month0 + 1, 1).strftime('%b'), 'year': year,
                 'employed': None, 'available': None, 'total': None, 'has_history': False}
        if offset == 0:
            point.update(employed=live_metrics['totalEmployed'], available=live_metrics['totalAvailable'],
                         total=live_metrics['totalWorkers'], has_history=True)
        elif history_since and history_since < boundary:
            latest = History.objects.filter(worker_id_snapshot=OuterRef('worker_id_snapshot'), changed_at__lt=boundary).order_by('-changed_at', '-id')
            entries = History.objects.filter(pk=Subquery(latest.values('pk')[:1]), is_verified=True)
            last_day = boundary.date() - timedelta(days=1)
            entries = [entry for entry in entries if (entry.clearance_valid_until is None or entry.clearance_valid_until >= last_day)
                       and matches_scope(entry.barangay, scope)]
            occupied = sum(entry.status for entry in entries)
            point.update(employed=occupied, available=len(entries) - occupied, total=len(entries), has_history=True)
        points.append(point)
    return {'trend': points, 'barangay': scope, 'history_since': history_since.isoformat() if history_since else None,
            'total_workers': live_metrics['totalWorkers'], 'current_occupied': live_metrics['totalEmployed'],
            'current_available': live_metrics['totalAvailable']}
