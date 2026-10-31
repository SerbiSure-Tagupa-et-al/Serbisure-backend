"""Scoped, audited reviewer corrections. Review decisions are separate actions."""
import hashlib
import json
from datetime import date

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError

from accounts.admin_statistics import barangay_name
from accounts.models import tbl_user_profile
from accounts.workforce import latest_required_documents
from verifications.models import tbl_documents
from verifications.services.audit_logger import record_audit_log
from verifications.services.document_fields import CORRECTIONS_KEY, NUMBER_KEYS, merge_document_fields
from verifications.services.matching_service import detect_discrepancies

PROFILE_FIELDS = ('first_name', 'middle_name', 'last_name', 'date_of_birth')
DOCUMENT_FIELDS = (*PROFILE_FIELDS, 'document_number', 'date_issued', 'valid_until')


class CorrectionConflict(APIException):
    status_code = 409
    default_detail = 'These details changed while you were editing. Close and reopen the form to review the latest values.'


def editable_document(actor, document_id):
    try:
        document = tbl_documents.objects.select_related('user_profile').get(pk=document_id)
    except tbl_documents.DoesNotExist as exc:
        raise NotFound('Document not found.') from exc
    assert_jurisdiction(actor, document.user_profile)
    return document


def assert_jurisdiction(actor, user):
    if (not actor.is_active or actor.account_type not in ('Admin', 'Barangay')
            or user.account_type not in ('Homeowner', 'Kasambahay')):
        raise PermissionDenied('Only reviewers can correct resident identity details.')
    if actor.account_type == 'Barangay':
        own = barangay_name(actor.barangay).casefold()
        if not own or own != barangay_name(user.barangay).casefold():
            raise PermissionDenied('This person is outside your barangay.')


def json_value(value):
    return value.isoformat() if isinstance(value, date) else value


def correction_details(document, user=None):
    user = user or document.user_profile
    extracted = document.extracted_data or {}
    profile = {key: json_value(getattr(user, key)) for key in PROFILE_FIELDS}
    details = {key: str(extracted[key]) if extracted.get(key) is not None else None for key in PROFILE_FIELDS}
    details.update({key: json_value(getattr(document, key)) for key in ('document_number', 'date_issued', 'valid_until')})
    number = document.document_number or next((extracted[key] for key in NUMBER_KEYS if extracted.get(key)), None)
    details['document_number'] = str(number) if number is not None else None
    # Include the source image and analysis in the version so a renewal or OCR
    # result arriving after the form opened cannot receive stale corrections.
    version_data = [str(document.pk), document.document_type, user.account_type, user.barangay,
                    profile, details, extracted, document.document_url,
                    str(document.created_at), document.verification_status]
    version = hashlib.sha256(json.dumps(version_data, sort_keys=True, default=str).encode()).hexdigest()
    return {'version': version, 'profile': profile, 'document': details, 'document_type': document.document_type}


@transaction.atomic
def save_corrections(actor, document_id, values, request=None):
    source = editable_document(actor, document_id)
    # Document-first locks match the OCR and expiry workflows. Stable ordering
    # also allows corrections to update sibling mismatch flags atomically.
    documents = list(tbl_documents.objects.select_for_update().filter(
        user_profile_id=source.user_profile_id,
    ).order_by('pk'))
    document = next((doc for doc in documents if doc.pk == source.pk), None)
    if document is None:
        raise CorrectionConflict()
    user = tbl_user_profile.objects.select_for_update().get(pk=source.user_profile_id)
    assert_jurisdiction(actor, user)
    if document.pk not in {doc.pk for doc in latest_required_documents(user)}:
        raise CorrectionConflict('A newer document was submitted. Open its preview instead.')
    before = correction_details(document, user)
    if before['version'] != values['version']:
        raise CorrectionConflict()
    profile_patch = values.get('profile', {})
    document_patch = values.get('document', {})
    if document.document_type.startswith('national_id') and {'date_issued', 'valid_until'} & document_patch.keys():
        raise ValidationError({'document': 'PhilSys National IDs do not need issue or expiry dates.'})
    if {'date_issued', 'valid_until'} & document_patch.keys():
        issued = document_patch.get('date_issued', document.date_issued)
        expires = document_patch.get('valid_until', document.valid_until)
        if issued and expires and expires < issued:
            raise ValidationError({'document': 'Expiry cannot be before issue date. Check both dates.'})
    changes = []
    for section, patch in [('profile', profile_patch), ('document', document_patch)]:
        for key, value in patch.items():
            value = json_value(value)
            if before[section][key] != value:
                changes.append({'section': section, 'field': key, 'old': before[section][key], 'new': value})
    if not changes:
        return {'message': 'No changes to save.', 'changed': False, **before}
    changed_profile = {item['field']: profile_patch[item['field']] for item in changes if item['section'] == 'profile'}
    changed_document = {item['field']: item['new'] for item in changes if item['section'] == 'document'}
    if changed_profile:
        for key, value in changed_profile.items():
            setattr(user, key, value)
        user.save(update_fields=list(changed_profile))
    if changed_document:
        corrections = {**(document.extracted_data or {}).get(CORRECTIONS_KEY, {}), **changed_document}
        document.extracted_data = merge_document_fields(document.extracted_data, corrections, document.document_type)
        document.extracted_data[CORRECTIONS_KEY] = corrections
        for key in ('document_number', 'date_issued', 'valid_until'):
            if key in changed_document:
                setattr(document, key, document_patch[key])
        document.ocr_processed_at = timezone.now()
        document.ocr_processing_token = None
        document.save(update_fields=['extracted_data', 'document_number', 'date_issued', 'valid_until',
                                      'ocr_processed_at', 'ocr_processing_token'])
    for doc in documents:
        if changed_profile or doc.pk == document.pk:
            score, flags = detect_discrepancies(doc.extracted_data or {}, user)
            tbl_documents.objects.filter(pk=doc.pk).update(
                ocr_match_score=score, ocr_discrepancies=flags, ocr_processing_token=None,
            )
    audit = record_audit_log(
        document=document, actor=actor, target_user=user, action='UPDATED',
        previous_status=document.verification_status, new_status=document.verification_status,
        reason='Reviewer corrected identity/document details.', request=request,
        metadata={'reason_code': 'reviewer_correction', 'changes': changes},
    )
    if audit is None:
        raise APIException('Could not record these corrections. No changes were saved.')
    return {'message': 'Corrections saved. Review decision is unchanged.', 'changed': True,
            **correction_details(document, user)}
