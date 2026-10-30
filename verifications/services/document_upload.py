import cloudinary.uploader
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException

from verifications.models import tbl_documents
from verifications.services import document_processor


class DocumentAlreadySubmitted(APIException):
    status_code = 409
    default_detail = 'This document is already submitted. Refresh your documents before trying again.'


def create_uploaded_document(user, image_file, document_data):
    """Store the image first; OCR/AI is exclusively a post-commit background job."""
    upload = cloudinary.uploader.upload(
        image_file, folder='serbisure_credentials/', type='authenticated',
    )
    public_id = upload['public_id']
    # These fields belong to this image. A replacement must not inherit old
    # analysis, reviewer metadata, retries, or ownership of a running attempt.
    reset_fields = {
        'document_url': public_id,
        'verification_status': 'Pending',
        'verifyBy': None,
        'rejection_reason': None,
        'document_number': None,
        'date_issued': None,
        'valid_until': None,
        'ocr_raw_text': None,
        'extracted_data': {},
        'ocr_match_score': None,
        'ocr_discrepancies': [],
        'ocr_processed_at': None,
        'ocr_retry_count': 0,
        'ocr_processing_token': None,
        'ocr_last_attempt_at': None,
        'face_liveness_score': None,
    }
    with transaction.atomic():
        existing = tbl_documents.objects.select_for_update().filter(
            user_profile=user, document_type=document_data['document_type'],
        ).order_by('-created_at', '-pk').first()
        if existing and existing.verification_status in ('Pending', 'Verified'):
            raise DocumentAlreadySubmitted()
        if existing:
            for key, value in {**document_data, **reset_fields}.items():
                setattr(existing, key, value)
            existing.created_at = timezone.now()
            existing.save()
            document = existing
        else:
            document = tbl_documents.objects.create(
                user_profile=user, **{**document_data, **reset_fields},
            )
        transaction.on_commit(
            lambda: document_processor.process_document_async(str(document.pk), expected_url=public_id),
            robust=True,
        )
    return document
