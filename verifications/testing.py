"""Transaction-test cleanup for the application's immutable audit tables."""
from django.db import connection, transaction
from django.test import TransactionTestCase


class AuditTransactionTestCase(TransactionTestCase):
    def _fixture_teardown(self):
        if connection.vendor != 'postgresql':
            return super()._fixture_teardown()
        if (not connection.settings_dict['NAME'].startswith('test_')
                or connection.settings_dict['HOST'] not in ('localhost', '127.0.0.1', '::1')):
            raise RuntimeError('Audit cleanup requires a disposable local test database.')
        # Guards stay enabled in test bodies; bypass only Django's fixture flush.
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('''
                SELECT c.relname, t.tgname FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE NOT t.tgisinternal AND (t.tgtype & 32) <> 0
                AND n.nspname = current_schema()
            ''')
            guards = cursor.fetchall()
            quote = connection.ops.quote_name
            for table, trigger in guards:
                cursor.execute(f'ALTER TABLE {quote(table)} DISABLE TRIGGER {quote(trigger)}')
            try:
                return super()._fixture_teardown()
            finally:
                for table, trigger in guards:
                    cursor.execute(f'ALTER TABLE {quote(table)} ENABLE TRIGGER {quote(trigger)}')
