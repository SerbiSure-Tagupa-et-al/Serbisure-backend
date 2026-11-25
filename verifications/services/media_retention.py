"""Status-based image retention; existing images receive the full rollout grace period."""
from datetime import datetime, time, timedelta
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import cloudinary
import cloudinary.uploader
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from verifications.models import tbl_documents, tbl_audit_logs
from verifications.services.audit_logger import record_audit_log


def retention_start():
    value = settings.VERIFICATION_MEDIA_RETENTION_START_AT
    value = parse_datetime(value) if isinstance(value, str) else value
    if value is None or timezone.is_naive(value):
        raise ValueError('VERIFICATION_MEDIA_RETENTION_START_AT must include a timezone.')
    return value


def effective_since(value):
    return max(value or retention_start(), retention_start())


def status_since(document):
    action = {'Verified': 'APPROVED', 'Rejected': 'REJECTED', 'Pending': 'RESET'}.get(document.verification_status)
    event = document.audit_logs.filter(action=action, created_at__gte=document.created_at).order_by('-created_at').first() if action else None
    return effective_since(event.created_at if event else document.created_at)


def closed_since(user):
    event = tbl_audit_logs.objects.filter(
        target_user=user, metadata__reason_code='retention_account_closed',
    ).order_by('-created_at').first()
    # Legacy inactive accounts have no closure timestamp: the rollout grants a fresh grace period.
    return effective_since(event.created_at if event else max(user.date_joined, retention_start()))


def eligibility(document, now, days=30, pending_abandon_days=60):
    """Return (purge category, overdue pending). No data is changed."""
    if not document.document_url or document.media_purged_at:
        return None, False
    period = timedelta(days=days)
    if not document.user_profile.is_active and now >= closed_since(document.user_profile) + period:
        return 'closed_account', False
    if document.document_type == 'profile_photo':
        return None, False
    newer = tbl_documents.objects.filter(
        user_profile_id=document.user_profile_id, document_type=document.document_type,
    ).filter(Q(created_at__gt=document.created_at) | Q(created_at=document.created_at, pk__gt=document.pk)).order_by('created_at', 'pk').first()
    if newer and now >= effective_since(newer.created_at) + period:
        return 'replaced', False
    if document.verification_status in ('Verified', 'Rejected') and now >= status_since(document) + period:
        return ('verified' if document.verification_status == 'Verified' else 'rejected'), False
    if document.valid_until:
        expired_at = datetime.combine(document.valid_until + timedelta(days=1), time.min, ZoneInfo('Asia/Manila'))
        if now >= effective_since(expired_at) + period:
            return 'expired', False
    if document.verification_status in ('Pending', 'Unverified'):
        since = status_since(document)
        if now >= since + timedelta(days=pending_abandon_days):
            return 'abandoned_pending', False
        return None, now >= since + period
    return None, False


def retire_media(document, reason):
    """Keep the old asset reference when a submission is overwritten or its row is removed."""
    if not document.document_url or document.media_purged_at:
        return
    since = effective_since(timezone.now())
    if document.document_type != 'profile_photo' and document.verification_status in ('Verified', 'Rejected'):
        since = min(since, status_since(document))
    if document.document_type != 'profile_photo' and document.valid_until:
        expiry = datetime.combine(document.valid_until + timedelta(days=1), time.min, ZoneInfo('Asia/Manila'))
        since = min(since, effective_since(expiry))
    return tbl_audit_logs.objects.create(
        action='UPDATED', document_id_snapshot=document.pk, document_type=document.document_type,
        actor_name='System / Automated', actor_role='System', reason='Raw media queued for retention cleanup.',
        previous_status=document.verification_status, new_status=document.verification_status,
        metadata={'reason_code': 'retention_media_retired', 'public_id': document.document_url,
                  'retired_reason': reason, 'retention_since': since.isoformat()},
    )


def asset_identity(value):
    """Resolve IDs and signed URL aliases to the same configured Cloudinary asset."""
    public_id, delivery = value, 'authenticated'
    if value.startswith(('https://', 'http://')):
        parsed = urlparse(value)
        parts = parsed.path.strip('/').split('/')
        if parsed.hostname != 'res.cloudinary.com' or len(parts) < 5 or parts[0] != cloudinary.config().cloud_name or parts[1] != 'image':
            raise ValueError('Cannot safely identify this Cloudinary image.')
        delivery = parts[2]
        if delivery not in ('upload', 'authenticated', 'private'):
            raise ValueError('Unsupported Cloudinary delivery type.')
        # Generated URLs may include signatures/transformations before their version segment.
        version = next((i for i, part in enumerate(parts[3:], 3) if part.startswith('v') and part[1:].isdigit()), None)
        if version is None:
            raise ValueError('Cloudinary URL has no unambiguous version segment.')
        public_id = unquote('/'.join(parts[version + 1:])).rsplit('.', 1)[0]
    return public_id, delivery


def destroy_media(value):
    """Delete only a configured Cloudinary image, with cached delivery copies invalidated."""
    public_id, delivery = asset_identity(value)
    result = cloudinary.uploader.destroy(public_id, type=delivery, resource_type='image', invalidate=True, timeout=10)
    if result.get('result') not in ('ok', 'not found'):
        raise RuntimeError('Cloudinary did not confirm media deletion.')


def shared_media(value, excluding=None):
    from accounts.models import tbl_user_profile
    others = tbl_documents.objects.exclude(document_url='').filter(media_purged_at__isnull=True)
    if excluding:
        others = others.exclude(pk=excluding)
    target = asset_identity(value)
    avatars = tbl_user_profile.objects.filter(is_active=True).exclude(profile_link='').exclude(profile_link__isnull=True)
    for values in (others.values_list('document_url', flat=True), avatars.values_list('profile_link', flat=True)):
        for reference in values.iterator(chunk_size=200):
            try:
                if asset_identity(reference) == target:
                    return True
            except ValueError:
                continue
    return False


def purge_document(document_id, now, days, pending_abandon_days):
    with transaction.atomic():
        document = tbl_documents.objects.select_for_update().select_related('user_profile').get(pk=document_id)
        user = type(document.user_profile).objects.select_for_update().get(pk=document.user_profile_id)
        document.user_profile = user
        category, _ = eligibility(document, now, days, pending_abandon_days)
        if not category:
            return None
        old_url = document.document_url
        if shared_media(old_url, excluding=document.pk):
            return 'skipped_shared'
        destroy_media(old_url)
        updates = dict(document_url='', media_purged_at=now, ocr_processing_token=None,
                       ocr_raw_text=None, extracted_data={}, ocr_discrepancies=[], ocr_match_score=None)
        if category == 'abandoned_pending':
            updates.update(verification_status='Unverified', rejection_reason='The unreviewed image expired under the retention policy. Please upload a new document.')
        previous = document.verification_status
        tbl_documents.objects.filter(pk=document.pk).update(**updates)
        if not user.is_active and user.profile_link == old_url:
            type(user).objects.filter(pk=user.pk).update(profile_link='')
        log = record_audit_log(document=document, target_user=user, action='UPDATED', previous_status=previous,
            new_status=updates.get('verification_status', previous), reason='Raw image removed under the document retention policy.',
            metadata={'reason_code': 'retention_media_purged', 'category': category})
        if log is None:
            raise RuntimeError('Could not record the retention audit entry.')
        return category


def purge_retired(event_id, now, days):
    with transaction.atomic():
        event = tbl_audit_logs.objects.select_for_update().get(pk=event_id)
        since = parse_datetime((event.metadata or {}).get('retention_since', '')) or event.created_at
        if now < effective_since(since) + timedelta(days=days):
            return None
        if tbl_audit_logs.objects.filter(metadata__retired_event_id=str(event.pk), metadata__reason_code='retention_media_purged').exists():
            return None
        value = (event.metadata or {}).get('public_id')
        if not value or shared_media(value):
            return 'skipped_shared'
        destroy_media(value)
        from accounts.models import tbl_user_profile
        tbl_user_profile.objects.filter(is_active=False, profile_link=value).update(profile_link='')
        tbl_audit_logs.objects.create(action='UPDATED', document_id_snapshot=event.document_id_snapshot,
            document_type=event.document_type, actor_name='System / Automated', actor_role='System',
            reason='Retired raw image removed under the document retention policy.',
            metadata={'reason_code': 'retention_media_purged', 'retired_event_id': str(event.pk), 'category': 'retired'})
        return 'retired'
