from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('booking', '0018_remove_default_postal_code')]
    # Old databases contain these columns, but the current migration state does
    # not. No CASCADE: unexpected external dependencies must stop the migration.
    # Deliberately irreversible; restoring old values requires the SQL backup.
    operations = [migrations.RunSQL(
        'ALTER TABLE "tbl_booking" '
        'DROP COLUMN IF EXISTS "cancel_requested_at", '
        'DROP COLUMN IF EXISTS "cancel_requested_by";'
    )]
