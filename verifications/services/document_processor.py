import logging
from threading import Thread
import uuid
from datetime import date, timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

from notifications.models import tbl_notification
from verifications.models import tbl_documents
from verifications.services.ocr_service import (
    is_google_vision_available,
    extract_text_with_google_vision,
    is_tesseract_available,
    fetch_image_from_cloudinary,
    extract_text_with_tesseract,
)
from verifications.services.groq_service import (
    extract_structured_data_from_text,
    extract_from_image_vision,
)
from verifications.services.matching_service import detect_discrepancies

logger = logging.getLogger(__name__)
MAX_AUTOMATIC_RETRIES = 3
RETRY_COOLDOWN = timedelta(seconds=30)
PROCESSING_LEASE = timedelta(minutes=5)


def _needs_analysis(document):
    return (
        document.ocr_processed_at is None
        or not document.extracted_data
        or not (document.ocr_raw_text or '').strip()
    )


def document_needs_retry(document, *, now=None, max_retries=MAX_AUTOMATIC_RETRIES):
    """A status read may recover failed work, but must not duplicate active work."""
    now = now or timezone.now()
    last_attempt = document.ocr_last_attempt_at or document.created_at
    active = document.ocr_processing_token and last_attempt > now - PROCESSING_LEASE
    return (
        document.verification_status == 'Pending'
        and document.ocr_retry_count < max_retries
        and _needs_analysis(document)
        and not active
        and last_attempt <= now - RETRY_COOLDOWN
    )


def _claim_document(document_id, *, retry=False, force=False, expected_url=None,
                    max_retries=MAX_AUTOMATIC_RETRIES):
    # Only this short claim uses a row lock. External API calls run after commit.
    with transaction.atomic():
        document = tbl_documents.objects.select_for_update().filter(pk=document_id).first()
        if document is None or (expected_url is not None and document.document_url != expected_url):
            return None
        now = timezone.now()
        if (document.ocr_processing_token and document.ocr_last_attempt_at
                and document.ocr_last_attempt_at > now - PROCESSING_LEASE):
            return None
        if retry:
            if not document_needs_retry(document, now=now, max_retries=max_retries):
                return None
        elif not force:
            if document.verification_status != 'Pending' or not _needs_analysis(document):
                return None
            if document.ocr_last_attempt_at and document.ocr_last_attempt_at > now - RETRY_COOLDOWN:
                return None
        document.ocr_processing_token = uuid.uuid4()
        document.ocr_last_attempt_at = now
        if retry:
            document.ocr_retry_count += 1
        tbl_documents.objects.filter(pk=document.pk).update(
            ocr_processing_token=document.ocr_processing_token,
            ocr_last_attempt_at=now,
            ocr_retry_count=document.ocr_retry_count,
        )
        return document


def _owned_document(document):
    # Both the image and attempt must still match. Re-upload or lease recovery
    # invalidates an old worker even if it finishes later.
    return tbl_documents.objects.filter(
        pk=document.pk, document_url=document.document_url,
        ocr_processing_token=document.ocr_processing_token,
    )


def _process_claimed_document(document):
    if not _owned_document(document).exists():
        return False
    image, raw_bytes = fetch_image_from_cloudinary(document.document_url)
    raw_text = ''
    extracted_data = {}
    if is_google_vision_available():
        try:
            raw_text = extract_text_with_google_vision(raw_bytes)
        except Exception:
            logger.warning('Primary OCR failed for document %s', document.pk)

    if not raw_text and is_tesseract_available():
        try:
            raw_text = extract_text_with_tesseract(image)
        except Exception:
            logger.warning('Fallback OCR failed for document %s', document.pk)

    if raw_text and len(raw_text.strip()) >= 15:
        extracted_data = extract_structured_data_from_text(raw_text, document.document_type)
    else:
        vision_text, extracted_data = extract_from_image_vision(raw_bytes, document.document_type)
        raw_text = vision_text or raw_text
    if not isinstance(extracted_data, dict):
        raise ValueError('Document extraction did not return an object')

    match_score, discrepancies = detect_discrepancies(extracted_data, document.user_profile)
    fields = {
        'ocr_raw_text': raw_text,
        'extracted_data': extracted_data,
        'ocr_match_score': match_score,
        'ocr_discrepancies': discrepancies,
        'ocr_processed_at': timezone.now(),
        'document_number': (
            extracted_data.get('clearance_number') or extracted_data.get('philsys_number')
            or extracted_data.get('document_number') or extracted_data.get('id_number')
        ),
    }
    for field in ('date_issued', 'valid_until'):
        if extracted_data.get(field):
            try:
                fields[field] = date.fromisoformat(str(extracted_data[field]))
            except (ValueError, TypeError):
                pass
    # Never save a stale model instance or write review fields. An official may
    # have approved/rejected it while OCR was running.
    with transaction.atomic():
        if not _owned_document(document).update(**fields):
            logger.info('Discarded stale analysis for document %s', document.pk)
            return False
        # Expiry dates affect credential eligibility even when an official has
        # already reviewed the document. Preserve the model's history side effect.
        from accounts.workforce import record_worker_status
        record_worker_status(document.user_profile_id, using=document._state.db)
    if _owned_document(document).filter(verification_status='Pending').exists():
        try:
            display = dict(tbl_documents.DOCUMENT_CHOICES).get(document.document_type, document.document_type)
            tbl_notification.objects.create(
                sender_id=document.user_profile, receiver_id=document.user_profile,
                notification_message=(
                    f'Your {display} was analyzed and is currently under review by Barangay/Admin officials.'
                ),
            )
        except Exception:
            logger.warning('Analysis notification failed for document %s', document.pk)
    return True


def _execute_claimed(document):
    try:
        return _process_claimed_document(document)
    except Exception:
        logger.exception('Document analysis failed for %s', document.pk)
        if document.ocr_retry_count >= MAX_AUTOMATIC_RETRIES:
            _owned_document(document).update(ocr_processed_at=timezone.now())
        return False
    finally:
        _owned_document(document).update(ocr_processing_token=None)


def process_document(document_id, *, retry=False, force=False, max_retries=MAX_AUTOMATIC_RETRIES):
    """Synchronous entry for the maintenance command; shares the same claim guard."""
    document = _claim_document(document_id, retry=retry, force=force, max_retries=max_retries)
    return _execute_claimed(document) if document is not None else None


def _run_in_background(document):
    close_old_connections()
    try:
        _execute_claimed(document)
    finally:
        close_old_connections()


def process_document_async(document_id, *, retry=False, force=False, expected_url=None,
                           max_retries=MAX_AUTOMATIC_RETRIES):
    """Claim once in the database, then process without blocking the upload."""
    document = _claim_document(
        document_id, retry=retry, force=force, expected_url=expected_url, max_retries=max_retries,
    )
    if document is None:
        return None
    try:
        thread = Thread(
            target=_run_in_background, args=(document,), daemon=True,
            name=f'DocProcessor-{document.pk}',
        )
        thread.start()
        return thread
    except Exception:
        _owned_document(document).update(ocr_processing_token=None)
        logger.exception('Could not start document analysis for %s', document.pk)
        return None

