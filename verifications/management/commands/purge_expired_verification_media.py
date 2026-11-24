import json
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from verifications.models import tbl_documents, tbl_audit_logs
from verifications.services.media_retention import effective_since, eligibility, purge_document, purge_retired, retention_start


class Command(BaseCommand):
    help = 'Purge due verification images while preserving verification records. Schedule daily; preview with --dry-run.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--days', type=int, default=30)
        parser.add_argument('--pending-abandon-days', type=int, default=60)

    def handle(self, *args, **options):
        days, abandon = options['days'], options['pending_abandon_days']
        if days < 30 or abandon < 60 or abandon <= days:
            raise CommandError('Use at least 30 retention days and 60 pending-abandon days; abandonment must be later than retention.')
        try:
            start = retention_start()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        now = timezone.now()
        eligible, purged = Counter(), Counter()
        overdue = skipped = errors = 0
        documents = tbl_documents.objects.exclude(document_url='').filter(media_purged_at__isnull=True).select_related('user_profile')
        for document in documents.iterator(chunk_size=200):
            category, pending_overdue = eligibility(document, now, days, abandon)
            overdue += int(pending_overdue)
            if category is None:
                skipped += 1
                continue
            eligible[category] += 1
            if options['dry_run']:
                continue
            try:
                result = purge_document(document.pk, now, days, abandon)
                if result == 'skipped_shared' or result is None:
                    skipped += 1
                else:
                    purged[result] += 1
            except Exception as exc:
                errors += 1
                self.stderr.write(f'Image cleanup failed for document {document.pk}: {type(exc).__name__}; retained for retry.')

        receipts = set(tbl_audit_logs.objects.filter(metadata__reason_code='retention_media_purged')
                       .values_list('metadata__retired_event_id', flat=True))
        retired = tbl_audit_logs.objects.filter(metadata__reason_code='retention_media_retired')
        for event in retired.iterator(chunk_size=200):
            since = parse_datetime((event.metadata or {}).get('retention_since', '')) or event.created_at
            if str(event.pk) in receipts or now < effective_since(since) + timedelta(days=days):
                continue
            eligible['retired'] += 1
            if options['dry_run']:
                continue
            try:
                result = purge_retired(event.pk, now, days)
                if result == 'skipped_shared' or result is None:
                    skipped += 1
                else:
                    purged[result] += 1
            except Exception as exc:
                errors += 1
                self.stderr.write(f'Image cleanup failed for retired event {event.pk}: {type(exc).__name__}; retained for retry.')
        summary = {'dry_run': options['dry_run'], 'retention_start_at': start.isoformat(),
                   'eligible_by_category': dict(eligible), 'purged_by_category': dict(purged),
                   'overdue_pending_flagged': overdue, 'skipped': skipped, 'errors': errors}
        self.stdout.write(json.dumps(summary, sort_keys=True))
        if errors:
            raise CommandError(f'{errors} image cleanup operation(s) failed; run again after resolving storage access.')
