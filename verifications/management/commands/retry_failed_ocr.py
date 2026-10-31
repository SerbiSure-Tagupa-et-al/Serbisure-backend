import logging
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from verifications.models import tbl_documents
from verifications.services.document_processor import document_needs_retry, process_document, process_document_async

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Finds pending documents whose OCR processing was interrupted or failed "
        "(ocr_processed_at IS NULL) and retriggers processing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-retries",
            type=int,
            default=3,
            help="Maximum retry attempts per document before giving up (default: 3).",
        )
        parser.add_argument(
            "--max-age-hours",
            type=int,
            default=48,
            help="Only retry documents created within the last N hours (default: 48).",
        )
        parser.add_argument(
            "--min-age-seconds",
            type=int,
            default=60,
            help="Minimum age in seconds before considering a document stuck (default: 60).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=25,
            help="Maximum number of documents to retry in a single run (default: 25).",
        )
        parser.add_argument(
            "--async",
            action="store_true",
            dest="use_async",
            help="Run processing in background daemon threads instead of sequentially.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Simulate the command without modifying the database or triggering OCR.",
        )

    def handle(self, *args, **options):
        max_retries = options["max_retries"]
        max_age_hours = options["max_age_hours"]
        min_age_seconds = options["min_age_seconds"]
        limit = options["limit"]
        use_async = options["use_async"]
        dry_run = options["dry_run"]

        now = timezone.now()
        min_created = now - timedelta(hours=max_age_hours)
        max_created = now - timedelta(seconds=min_age_seconds)

        self.stdout.write(
            f"Scanning for stuck/empty documents: "
            f"status=Pending, retries < {max_retries}, "
            f"age between {min_age_seconds}s and {max_age_hours}h..."
        )

        from django.db.models import Q

        stuck_docs = (
            tbl_documents.objects.filter(
                verification_status="Pending",
                ocr_retry_count__lt=max_retries,
                created_at__gte=min_created,
                created_at__lte=max_created,
            )
            .filter(
                Q(ocr_processed_at__isnull=True)
                | Q(extracted_data__isnull=True)
                | Q(extracted_data={})
                | Q(ocr_raw_text__isnull=True)
                | Q(ocr_raw_text="")
            )
            .order_by("created_at")[:limit]
        )

        stuck_docs = [doc for doc in stuck_docs if document_needs_retry(doc, now=now, max_retries=max_retries)]
        stuck_count = len(stuck_docs)
        if stuck_count == 0:
            self.stdout.write(self.style.SUCCESS("No stuck documents found. Everything is up to date!"))
            return

        self.stdout.write(
            self.style.WARNING(f"Found {stuck_count} stuck document(s) needing retry.")
        )

        retried = 0
        failed = 0

        for doc in stuck_docs:
            doc_id_str = str(doc.document_id)
            new_retry_count = doc.ocr_retry_count + 1

            self.stdout.write(
                f" - Document {doc_id_str} ({doc.document_type}), "
                f"attempt {new_retry_count}/{max_retries}..."
            )

            if dry_run:
                self.stdout.write(self.style.NOTICE("   [DRY RUN] Would retry."))
                continue

            try:
                if use_async:
                    result = process_document_async(doc_id_str, retry=True, max_retries=max_retries)
                else:
                    result = process_document(doc_id_str, retry=True, max_retries=max_retries)
                if result is None:
                    self.stdout.write(self.style.NOTICE("   Skipped: another attempt already owns this document."))
                    continue
                if result is False:
                    failed += 1
                    self.stdout.write(self.style.WARNING("   Analysis failed; upload remains available for review."))
                    continue
                self.stdout.write(self.style.SUCCESS("   Enqueued in background thread." if use_async else "   Processed successfully."))
                retried += 1
            except Exception as exc:
                failed += 1
                logger.error(f"[RetryCommand] Error processing document {doc_id_str}: {exc}", exc_info=True)
                self.stdout.write(self.style.ERROR(f"   Failed to process: {exc}"))

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry run complete. {stuck_count} documents identified."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Retry run completed: {retried} retried, {failed} failed out of {stuck_count}."
                )
            )
