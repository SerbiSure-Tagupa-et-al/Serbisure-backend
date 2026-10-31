"""Exercise real PostgreSQL schema changes with populated legacy records."""
from datetime import timedelta
from importlib import import_module

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone
from verifications.testing import AuditTransactionTestCase


class ColumnCleanupMigrationTests(AuditTransactionTestCase):
    before = [('accounts', '0024_penalty_adjustment'),
              ('booking', '0018_remove_default_postal_code'),
              ('chat', '0011_message_visibility_per_participant'),
              ('verifications', '0010_document_processing_lease')]

    def test_populated_legacy_schema_upgrades_and_current_writes_work(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        # Its state is unchanged and the test DB is already clean. Unrecord the
        # irreversible database-only operation so the historical fixture can be
        # installed, then recreate the old columns explicitly below.
        MigrationRecorder(connection).record_unapplied('booking', '0019_remove_legacy_cancel_requests')
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        try:
            apps = executor.loader.project_state(self.before).apps
            User = apps.get_model('accounts', 'tbl_user_profile')
            owner = User.objects.create(username='cleanup-owner', email='cleanup-owner@test.com',
                contact_number='+639111112211', first_name='Owner', last_name='Test',
                resume_url='legacy.pdf', resume_uploaded_at=timezone.now())
            worker = User.objects.create(username='cleanup-worker', email='cleanup-worker@test.com',
                contact_number='+639111112212', first_name='Worker', last_name='Test')
            Booking = apps.get_model('booking', 'tbl_booking')
            booking = Booking.objects.create(poster_id=owner, booking_type='short_term',
                booking_status='Pending', service_category=['Cleaning'], rate_amount=600,
                rate_period='day', start_time=timezone.now() + timedelta(days=2),
                cancellation_reason='Retained reason')
            # These database-only columns are missing on a clean installation,
            # but exist in the production backup. Simulate that older schema.
            with connection.cursor() as cursor:
                cursor.execute('ALTER TABLE tbl_booking ADD COLUMN cancel_requested_at timestamptz, '
                    'ADD COLUMN cancel_requested_by uuid REFERENCES accounts_tbl_user_profile(id)')
                cursor.execute('UPDATE tbl_booking SET cancel_requested_at=NOW(), cancel_requested_by=%s '
                    'WHERE booking_id=%s', [owner.pk, booking.pk])
            Document = apps.get_model('verifications', 'tbl_documents')
            document = Document.objects.create(user_profile=owner, document_type='national_id_front',
                document_url='existing-image', ocr_match_score=.85, face_liveness_score=99.2)
            # Preserve coverage of the old normalization migration using its
            # historical model, rather than accessing a removed field on today's model.
            with connection.schema_editor() as editor:
                import_module('verifications.migrations.0009_normalize_document_scores').normalize_scores(apps, editor)
            document.refresh_from_db()
            self.assertEqual(document.ocr_match_score, 85)
            self.assertIsNone(document.face_liveness_score)
            Message = apps.get_model('chat', 'tbl_chat_message')
            message = Message.objects.create(sender_id=owner, receiver_id=worker, message_payload='Hello')
            Reaction = apps.get_model('chat', 'tbl_chat_reaction')
            reaction = Reaction.objects.create(message=message, reactor=worker, emoji='👍')
        finally:
            MigrationExecutor(connection).migrate(latest)

        removed = {'accounts_tbl_user_profile': {'resume_url', 'resume_uploaded_at'},
                   'tbl_booking': {'cancel_requested_at', 'cancel_requested_by'},
                   'tbl_documents': {'face_liveness_score'}, 'tbl_chat_reaction': {'reacted_at'}}
        with connection.cursor() as cursor:
            for table, fields in removed.items():
                columns = {column.name for column in connection.introspection.get_table_description(cursor, table)}
                self.assertFalse(columns & fields, table)
        from accounts.models import tbl_user_profile
        from booking.models import tbl_booking
        from chat.models import tbl_chat_reaction
        from verifications.models import tbl_documents
        self.assertTrue(tbl_user_profile.objects.filter(pk=owner.pk).exists())
        self.assertEqual(tbl_booking.objects.get(pk=booking.pk).cancellation_reason, 'Retained reason')
        self.assertEqual(tbl_documents.objects.get(pk=document.pk).document_url, 'existing-image')
        self.assertEqual(tbl_chat_reaction.objects.get(pk=reaction.pk).emoji, '👍')
        tbl_chat_reaction.objects.filter(pk=reaction.pk).delete()
        tbl_chat_reaction.objects.create(message_id=message.pk, reactor_id=worker.pk, emoji='❤️')
        # Repeating the database-only cleanup is safe on a fresh/already-clean schema.
        migration = import_module('booking.migrations.0019_remove_legacy_cancel_requests')
        with connection.schema_editor() as editor:
            migration.Migration.operations[0].database_forwards('booking', editor, None, None)
