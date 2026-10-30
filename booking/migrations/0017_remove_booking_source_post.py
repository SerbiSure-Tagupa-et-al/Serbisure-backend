from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('booking', '0016_booking_source_post')]
    operations = [migrations.RemoveField(model_name='tbl_booking', name='source_post')]
