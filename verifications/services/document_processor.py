import logging
import threading
from datetime import date
from django.db import close_old_connections
from django.utils import timezone
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
from notifications.models import tbl_notification

logger = logging.getLogger(__name__)


def process_document(document_id: str):
    """
    Executes the full document processing pipeline:
    1. Fetch image from Cloudinary
    2. Extract text with Google Cloud Vision API (or Tesseract / Groq Vision fallback)
    3. Structure fields with Groq LLM (llama-3.3-70b-versatile)
    4. Cross-check against user profile to compute similarity and detect discrepancies
    5. Save results to tbl_documents
    6. Send in-app notification to user
    """
    try:
        document = tbl_documents.objects.select_related("user_profile").get(document_id=document_id)
    except tbl_documents.DoesNotExist:
        logger.error(f"[DocProcessor] Document with id {document_id} not found.")
        return

    logger.info(f"[DocProcessor] Starting OCR & AI processing for document {document_id} ({document.document_type})")

    try:
        img, raw_bytes = fetch_image_from_cloudinary(document.document_url)
    except Exception as e:
        logger.error(f"[DocProcessor] Failed to fetch image from Cloudinary for {document_id}: {e}")
        document.ocr_processed_at = timezone.now()
        document.save(update_fields=["ocr_processed_at"])
        return

    raw_text = ""
    extracted_data = {}

    # Step 1: Google Cloud Vision API (Primary OCR engine)
    if is_google_vision_available():
        try:
            logger.info(f"[DocProcessor] Running Google Cloud Vision OCR on {document_id}...")
            raw_text = extract_text_with_google_vision(raw_bytes)
            logger.info(f"[DocProcessor] Google Vision extracted {len(raw_text)} characters.")
        except Exception as e:
            logger.warning(f"[DocProcessor] Google Vision failed on {document_id}: {e}")
            raw_text = ""

    # Step 2: Fallback to Tesseract OCR if Google Vision didn't run or failed
    if not raw_text and is_tesseract_available():
        try:
            logger.info(f"[DocProcessor] Running Tesseract OCR fallback on {document_id}...")
            raw_text = extract_text_with_tesseract(img)
            logger.info(f"[DocProcessor] Tesseract extracted {len(raw_text)} characters.")
        except Exception as e:
            logger.warning(f"[DocProcessor] Tesseract fallback failed on {document_id}: {e}")
            raw_text = ""

    # Step 3: Extract structured data with Groq Llama-3.3
    if raw_text and len(raw_text.strip()) >= 15:
        logger.info(f"[DocProcessor] Using Groq Llama-3.3 to structure OCR text...")
        extracted_data = extract_structured_data_from_text(raw_text, document.document_type)
    else:
        # Fallback to Groq Vision if both OCR engines didn't extract text
        logger.info(f"[DocProcessor] Fallback to Groq Vision for {document_id}...")
        vision_text, vision_data = extract_from_image_vision(raw_bytes, document.document_type)
        if vision_text:
            raw_text = vision_text
        if vision_data:
            extracted_data = vision_data

    # Step 3: Populate extracted dates on document if found
    if extracted_data.get("date_issued") and not document.date_issued:
        try:
            document.date_issued = date.fromisoformat(str(extracted_data["date_issued"]))
        except (ValueError, TypeError):
            pass

    if extracted_data.get("valid_until") and not document.valid_until:
        try:
            document.valid_until = date.fromisoformat(str(extracted_data["valid_until"]))
        except (ValueError, TypeError):
            pass

    # Step 4: Discrepancy detection & profile matching
    match_score, discrepancies = detect_discrepancies(extracted_data, document.user_profile)

    document.ocr_raw_text = raw_text
    document.extracted_data = extracted_data
    document.ocr_match_score = match_score
    document.ocr_discrepancies = discrepancies
    document.ocr_processed_at = timezone.now()
    # Verification status remains 'Pending' for manual admin/barangay review
    document.verification_status = "Pending"
    document.save()

    logger.info(
        f"[DocProcessor] Completed processing document {document_id}. "
        f"Match Score: {match_score}, Discrepancies: {len(discrepancies)}"
    )

    # Step 5: Send notification to user that processing is complete and pending manual review
    try:
        doc_display = dict(tbl_documents.DOCUMENT_CHOICES).get(document.document_type, document.document_type)
        tbl_notification.objects.create(
            sender_id=document.user_profile,
            receiver_id=document.user_profile,
            notification_message=(
                f"Your {doc_display} was analyzed and is currently under review by Barangay/Admin officials."
            ),
        )
    except Exception as e:
        logger.warning(f"[DocProcessor] Failed to create in-app notification: {e}")


def _run_in_background(document_id: str):
    """Worker function for background daemon thread with safe connection lifecycle."""
    close_old_connections()
    try:
        process_document(document_id)
    except Exception as e:
        logger.error(f"[DocProcessor] Background task error for {document_id}: {e}", exc_info=True)
    finally:
        close_old_connections()


def process_document_async(document_id: str):
    """
    Launch document processing in a daemon background thread so the HTTP request
    returns immediately without blocking the mobile user.
    """
    thread = threading.Thread(
        target=_run_in_background,
        args=(str(document_id),),
        daemon=True,
        name=f"DocProcessor-{document_id}",
    )
    thread.start()
    return thread

