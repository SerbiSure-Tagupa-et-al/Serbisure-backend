from django.db import models
from django.conf import settings
from django.db.models import CheckConstraint, Q
import uuid 

# Create your models here.

class tbl_documents(models.Model):


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
        help_text="0.0-1.0 score indicating how well OCR data matches profile"
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

    rejection_reason = models.TextField(
        blank=True,
        null=True,
        help_text="Reason for document rejection by Admin/Barangay"
    )

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

