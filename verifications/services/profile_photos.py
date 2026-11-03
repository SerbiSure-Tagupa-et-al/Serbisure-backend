"""Initial registration selfies and rejected-photo retakes share one review record."""
from django.db import transaction
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import APIException, ValidationError
from django.shortcuts import get_object_or_404

from verifications.models import tbl_documents
from verifications.services.audit_logger import record_audit_log


def current_profile_photo(user):
    return user.documents.filter(document_type='profile_photo').order_by('-created_at', '-pk').first()


def check_photo_upload(user, document=None):
    if user.account_type not in ('Homeowner', 'Kasambahay'):
        return
    document = document or current_profile_photo(user)
    if (document and document.verification_status != 'Rejected') or (not document and user.profile_link):
        raise PermissionDenied('Your photo is saved. You can retake it only if a reviewer asks you to.')


def save_photo_submission(user, public_id):
    with transaction.atomic():
        # Match document review's document -> account lock order.
        user.documents.select_for_update().filter(document_type='profile_photo').order_by('-created_at', '-pk').first()
        locked_user = type(user).objects.select_for_update().get(pk=user.pk)
        document = current_profile_photo(locked_user)
        check_photo_upload(locked_user, document)
        if document:
            previous = document.verification_status
            document.document_url = public_id
            document.verification_status = 'Pending'
            document.rejection_reason = None
            document.verifyBy = None
            document.save(update_fields=['document_url', 'verification_status', 'rejection_reason', 'verifyBy'])
            log = record_audit_log(document=document, action='UPLOADED', actor=locked_user,
                             target_user=locked_user, previous_status=previous, new_status='Pending',
                             reason='New face photo submitted for review.')
            if not log:
                raise APIException('Your photo could not be saved. Please try again.')
        else:
            # The existing creation signal records UPLOADED exactly once.
            document = tbl_documents.objects.create(user_profile=locked_user, document_type='profile_photo',
                                                     document_url=public_id, verification_status='Pending')
            if not document.audit_logs.filter(action='UPLOADED').exists():
                raise APIException('Your photo could not be saved. Please try again.')
        user.profile_link = public_id
        return document


def review_profile_photo(document_id, reviewer, action, reason, request):
    from accounts.admin_statistics import barangay_name
    from notifications.models import tbl_notification
    with transaction.atomic():
        document = get_object_or_404(tbl_documents.objects.select_for_update().select_related('user_profile'),
                                     pk=document_id, document_type='profile_photo')
        user = document.user_profile
        if reviewer.account_type == 'Barangay' and (
            not barangay_name(reviewer.barangay) or
            barangay_name(reviewer.barangay).casefold() != barangay_name(user.barangay).casefold()
        ):
            raise PermissionDenied('This photo is outside your barangay.')
        if current_profile_photo(user).pk != document.pk:
            raise ValidationError('Please refresh and review the current photo.')
        if action == 'reject' and (not isinstance(reason, str) or not reason.strip()):
            raise ValidationError({'rejection_reason': 'Please explain why the photo needs to be retaken.'})
        previous = document.verification_status
        document.verification_status = {'approve': 'Verified', 'reject': 'Rejected', 'reset': 'Pending'}[action]
        document.rejection_reason = reason.strip() if action == 'reject' else None
        document.verifyBy = reviewer if action != 'reset' else None
        document.save(update_fields=['verification_status', 'rejection_reason', 'verifyBy'])
        log = record_audit_log(document=document, actor=reviewer, target_user=user, request=request,
                               action={'approve': 'APPROVED', 'reject': 'REJECTED', 'reset': 'RESET'}[action],
                               previous_status=previous, new_status=document.verification_status,
                               reason=document.rejection_reason)
        if not log:
            raise APIException('The review could not be saved. Please try again.')
        message = (f'Please retake your profile photo: {document.rejection_reason}' if action == 'reject'
                   else 'Your profile photo has been approved.' if action == 'approve'
                   else 'Your profile photo is waiting for review.')
        tbl_notification.objects.create(sender_id=reviewer, receiver_id=user, notification_message=message)
        return document
