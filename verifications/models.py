from django.db import models, transaction
from django.conf import settings
from django.db.models import CheckConstraint, Q
import uuid 

# Create your models here.

class tbl_documents(models.Model):

    def save(self, *args, **kwargs):
        with transaction.atomic(using=kwargs.get('using') or 'default'):
            super().save(*args, **kwargs)
            from accounts.workforce import record_worker_status
            record_worker_status(self.user_profile_id, using=self._state.db)


    DOCUMENT_CHOICES = (
        ('nbi_clearance', 'NBI Clearance'),
        ('police_clearance', 'Police Clearance'),
        ('national_id_front', 'National ID (Front)'),
        ('national_id_back', 'National ID (Back)')
    )

    VERIFICATION_STATUS_CHOICES = (
        ('Unverified', 'Unverified'),
        ('Pending', 'Pending'),
        ('Verified', 'Verified'),
        ('Rejected', 'Rejected')
    )

    document_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    user_profile = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="documents"
    )

    verifyBy = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_documents", 
        limit_choices_to={'account_type__in': ['Admin', 'Barangay']}
    )

    document_type = models.CharField(
        max_length=100,
        choices=DOCUMENT_CHOICES,
        blank=False,
        null=False,
    )

    document_url = models.CharField(
        max_length=255,
        blank=False,
        null=False
    )

    date_issued = models.DateField(
        blank=True,
        null=True
    )

    valid_until = models.DateField(
        blank=True,
        null=True
    )

    verification_status = models.CharField(
        max_length=20,
        choices=VERIFICATION_STATUS_CHOICES,
        default='Pending'
    )

    extracted_data = models.JSONField(
        blank=True,
        null=True,
        default=dict,
        help_text="Structured data extracted by Groq AI from OCR text"
    )

    ocr_raw_text = models.TextField(
        blank=True,
        null=True,
        help_text="Raw text extracted by OCR engine"
    )

    ocr_match_score = models.FloatField(
        blank=True,
        null=True,
        help_text="Document text matching percentage (0-100); null when evidence is unavailable"
    )

    ocr_discrepancies = models.JSONField(
        default=list,
        blank=True,
        help_text="List of field mismatches between OCR output and profile data"
    )

    ocr_processed_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="When OCR + AI processing completed"
    )

    ocr_retry_count = models.PositiveSmallIntegerField(
        default=0,
        help_text="Number of times OCR processing has been retried"
    )

    ocr_processing_token = models.UUIDField(null=True, blank=True, editable=False)
    ocr_last_attempt_at = models.DateTimeField(null=True, blank=True, editable=False)

    rejection_reason = models.TextField(
        blank=True,
        null=True,
        help_text="Reason for document rejection by Admin/Barangay"
    )

    document_number = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    face_liveness_score = models.FloatField(
        blank=True,
        null=True
    )

    @property
    def ocr_extracted_data(self):
        return self.extracted_data

    @ocr_extracted_data.setter
    def ocr_extracted_data(self, value):
        self.extracted_data = value


    created_at = models.DateTimeField(
        auto_now_add=True
    )

    class Meta:

        db_table = 'tbl_documents'
        constraints = [

            CheckConstraint(
                condition=Q(document_type__in=['nbi_clearance', 'police_clearance', 'national_id_front', 'national_id_back']),
                name='valid_document_type_enum'
            ),

            CheckConstraint(
                condition=Q(verification_status__in=['Unverified', 'Pending', 'Verified', 'Rejected']),
                name='valid_document_verification_status_enum'
            )
        ]


class tbl_audit_logs(models.Model):
    """
    Immutable audit log table tracking document verification lifecycle:
    approvals, rejections, resets, uploads, deletions, and OCR reprocessing.
    Preserves historical identity even if users or documents are deleted.
    """
    ACTION_CHOICES = (
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
        ('RESET', 'Reset to Pending'),
        ('DELETED', 'Deleted'),
        ('UPLOADED', 'Uploaded'),
        ('REPROCESSED', 'Reprocessed'),
        ('UPDATED', 'Updated'),
    )

    log_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    # Actor (Officer, Admin, or System who performed the action)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_actions_performed"
    )
    actor_name = models.CharField(max_length=255, blank=True, null=True)
    actor_email = models.CharField(max_length=255, blank=True, null=True)
    actor_role = models.CharField(max_length=50, blank=True, null=True)
    actor_barangay = models.CharField(max_length=100, blank=True, null=True)

    # Target resident (Homeowner / Kasambahay applicant)
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_actions_received"
    )
    target_name = models.CharField(max_length=255, blank=True, null=True)
    target_email = models.CharField(max_length=255, blank=True, null=True)
    target_role = models.CharField(max_length=50, blank=True, null=True)
    target_barangay = models.CharField(max_length=100, blank=True, null=True)

    # Document details (with snapshot UUID in case row is deleted)
    document = models.ForeignKey(
        'tbl_documents',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs"
    )
    document_id_snapshot = models.UUIDField(null=True, blank=True, help_text="Preserved document UUID")
    document_type = models.CharField(max_length=100, blank=True, null=True)
    document_number = models.CharField(max_length=100, blank=True, null=True)

    # Action & status transitions
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    previous_status = models.CharField(max_length=50, blank=True, null=True)
    new_status = models.CharField(max_length=50, blank=True, null=True)
    reason = models.TextField(blank=True, null=True, help_text="Rejection reason or action remarks")

    # Security & network telemetry
    ip_address = models.GenericIPAddressField(blank=True, null=True)
    user_agent = models.CharField(max_length=500, blank=True, null=True)
    metadata = models.JSONField(default=dict, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'tbl_audit_logs'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['-created_at']),
            models.Index(fields=['action']),
            models.Index(fields=['actor_role']),
            models.Index(fields=['target_barangay']),
        ]

    def __str__(self):
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {self.actor_name or 'System'} {self.action} for {self.target_name or 'Resident'} ({self.document_type})"


