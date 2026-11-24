import logging
from django.db.models.signals import pre_save, pre_delete, post_save, post_delete
from django.conf import settings
from django.dispatch import receiver
from django.utils import timezone
from datetime import timedelta
from verifications.models import tbl_documents, tbl_audit_logs
from verifications.services.audit_logger import record_audit_log

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=settings.AUTH_USER_MODEL)
def retention_account_closed(sender, instance, using, **kwargs):
    if instance.pk and not instance.is_active and sender.objects.using(using).filter(pk=instance.pk, is_active=True).exists():
        log = record_audit_log(target_user=instance, action='UPDATED', previous_status='Active', new_status='Inactive',
            reason='Account closed; raw media retention period started.', metadata={'reason_code': 'retention_account_closed'})
        if log is None:
            raise RuntimeError('Could not record the account closure retention timestamp.')
        if instance.profile_link and not instance.documents.filter(document_url=instance.profile_link).exists():
            tbl_audit_logs.objects.using(using).create(action='UPDATED', actor_name='System / Automated', actor_role='System',
                document_type='profile_photo', reason='Closed account avatar queued for retention cleanup.',
                metadata={'reason_code': 'retention_media_retired', 'public_id': instance.profile_link, 'retired_reason': 'closed_account'})


@receiver(post_save, sender=tbl_documents)
def document_post_save_audit(sender, instance, created, **kwargs):
    """
    Automatically logs newly uploaded documents.
    """
    if created:
        try:
            record_audit_log(
                document=instance,
                action='UPLOADED',
                actor=getattr(instance, 'user_profile', None),
                target_user=getattr(instance, 'user_profile', None),
                previous_status=None,
                new_status=getattr(instance, 'verification_status', 'Pending'),
                reason="Document uploaded and queued for verification."
            )
        except Exception as e:
            logger.error(f"[Signal] Error in document_post_save_audit: {e}", exc_info=True)


@receiver(pre_delete, sender=tbl_documents)
def retain_deleted_media_reference(sender, instance, **kwargs):
    # Capture decision dates before deletion clears the audit log's document FK.
    from .services.media_retention import retire_media
    retire_media(instance, 'deleted')


@receiver(post_delete, sender=tbl_documents)
def document_post_delete_audit(sender, instance, **kwargs):
    """
    Automatically logs document deletions as a safety net if not already logged by a view.
    """
    try:
        doc_id = getattr(instance, 'document_id', None)
        if not doc_id:
            return

        # Check if an explicit DELETED audit log was already recorded for this document recently
        recent_threshold = timezone.now() - timedelta(seconds=3)
        already_logged = tbl_audit_logs.objects.filter(
            document_id_snapshot=doc_id,
            action='DELETED',
            created_at__gte=recent_threshold
        ).exists()

        if not already_logged:
            log = record_audit_log(
                document=None,
                action='DELETED',
                actor=None,
                target_user=getattr(instance, 'user_profile', None),
                previous_status=getattr(instance, 'verification_status', 'Deleted'),
                new_status='Deleted',
                reason="Document was removed from database.",
                document_snapshot={
                    'document_id': doc_id,
                    'document_type': getattr(instance, 'document_type', None),
                    'document_number': getattr(instance, 'document_number', None),
                }
            )
            # The deletion collector already cleared older audit FKs. New cascade
            # logs must keep their snapshots without reattaching to the removed user.
            from accounts.models import tbl_user_profile
            origin = kwargs.get('origin')
            if log and (isinstance(origin, tbl_user_profile) or getattr(origin, 'model', None) is tbl_user_profile):
                tbl_audit_logs.objects.filter(pk=log.pk).update(target_user=None)
    except Exception as e:
        logger.error(f"[Signal] Error in document_post_delete_audit: {e}", exc_info=True)
