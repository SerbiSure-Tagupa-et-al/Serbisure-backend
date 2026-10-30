from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('booking', '0015_booking_cancellation_details')]
    operations = [migrations.AddField(
        model_name='tbl_booking', name='source_post',
        field=models.ForeignKey(blank=True, editable=False, null=True,
            on_delete=django.db.models.deletion.PROTECT, related_name='source_offers', to='booking.tbl_booking'),
    )]
