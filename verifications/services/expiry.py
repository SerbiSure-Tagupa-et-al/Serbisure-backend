"""Officer-triggered clearance renewal. Reads never change review decisions."""
import hashlib
import json

from django.core import signing
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from accounts.admin_statistics import barangay_name, matches_scope
from accounts.models import tbl_user_profile
from accounts.workforce import PH_TIME
from notifications.models import tbl_notification
from verifications.models import tbl_documents
from verifications.services.audit_logger import record_audit_log

SALT = 'verifications.expired-clearances.v1'
CONFIRMATION_SECONDS = 300
CLEARANCES = ('nbi_clearance', 'police_clearance')


class ExpiryPreviewChanged(APIException):
    status_code = 409
    default_detail = 'The expired-document list changed. Preview and confirm again.'


def officer_scope(actor):
    if not actor.is_active or actor.account_type not in ('Admin', 'Barangay'):
        raise PermissionDenied('Only active admins and barangay officials can process expirations.')
    if actor.account_type == 'Admin':
        return 'All Barangays'
    if not barangay_name(actor.barangay):
        raise PermissionDenied('Your account has no assigned barangay.')
    return barangay_name(actor.barangay)


def expired_candidates(actor, today):
    scope = officer_scope(actor)
    users = tbl_user_profile.objects.filter(account_type='Kasambahay', is_active=True)
    if actor.account_type == 'Barangay':
        ids = [pk for pk, barangay in users.values_list('pk', 'barangay') if matches_scope(barangay, scope)]
        users = users.filter(pk__in=ids)
    latest = tbl_documents.objects.filter(
        user_profile_id=OuterRef('user_profile_id'), document_type=OuterRef('document_type'),
    ).order_by('-created_at', '-pk').values('pk')[:1]
    return tbl_documents.objects.filter(
        pk=Subquery(latest), user_profile__in=users, document_type__in=CLEARANCES,
        valid_until__lt=today,
    ).exclude(verification_status='Rejected').select_related('user_profile').order_by('pk')


def fingerprint(documents):
    values = [(str(doc.pk), str(doc.user_profile_id), doc.document_type, doc.document_url,
               doc.verification_status, str(doc.valid_until), str(doc.date_issued), str(doc.created_at))
              for doc in documents]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def preview_expirations(actor):
    today = timezone.localdate(timezone=PH_TIME)
    scope = officer_scope(actor)
    documents = list(expired_candidates(actor, today))
    token = signing.dumps({'actor': str(actor.pk), 'scope': scope, 'date': str(today),
                           'fingerprint': fingerprint(documents)}, salt=SALT)
    return {'scope': scope, 'as_of': str(today), 'document_count': len(documents),
            'worker_count': len({doc.user_profile_id for doc in documents}), 'confirmation_token': token}


@transaction.atomic
def reject_expired_clearances(actor, token, request=None):
    try:
        preview = signing.loads(token, salt=SALT, max_age=CONFIRMATION_SECONDS)
    except signing.BadSignature as exc:
        raise ValidationError({'confirmation_token': 'Preview expired or invalid. Preview again.'}) from exc
    scope = officer_scope(actor)
    if preview.get('actor') != str(actor.pk) or preview.get('scope') != scope:
        raise PermissionDenied('This confirmation belongs to another officer or jurisdiction.')
    today = timezone.localdate(timezone=PH_TIME)
    documents = list(expired_candidates(actor, today).select_for_update(of=('self',)))
    owners = {user.pk: user for user in tbl_user_profile.objects.select_for_update().filter(
        pk__in={doc.user_profile_id for doc in documents},
    ).order_by('pk')}
    if any(not owners[doc.user_profile_id].is_active
           or owners[doc.user_profile_id].account_type != 'Kasambahay'
           or not matches_scope(owners[doc.user_profile_id].barangay, scope) for doc in documents):
        raise ExpiryPreviewChanged()
    if preview.get('date') != str(today) or preview.get('fingerprint') != fingerprint(documents):
        raise ExpiryPreviewChanged()
    for doc in documents:
        previous = doc.verification_status
        label = dict(tbl_documents.DOCUMENT_CHOICES)[doc.document_type]
        reason = f'{label} expired on {doc.valid_until:%B %d, %Y}. Please upload your renewed clearance for review.'
        doc.verification_status = 'Rejected'
        doc.rejection_reason = reason
        doc.verifyBy = actor
        doc.save(update_fields=['verification_status', 'rejection_reason', 'verifyBy'])
        audit = record_audit_log(document=doc, actor=actor, target_user=doc.user_profile,
                                 action='REJECTED', previous_status=previous, new_status='Rejected',
                                 reason=reason, request=request,
                                 metadata={'reason_code': 'clearance_expired', 'valid_until': str(doc.valid_until),
                                           'scope': scope, 'manual_expiration_action': True})
        if audit is None:
            raise APIException('Could not record the expiration decision. No documents were changed.')
        # The decision, audit, and renewal notification either all commit or all roll back.
        tbl_notification.objects.create(sender_id=actor, receiver_id=doc.user_profile,
                                         notification_message=reason)
    return {'scope': scope, 'document_count': len(documents),
            'worker_count': len({doc.user_profile_id for doc in documents}),
            'message': 'Expired clearances marked Rejected. Workers can now upload renewals.'}
