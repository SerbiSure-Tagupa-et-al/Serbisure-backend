from django.core.validators import RegexValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('booking', '0017_remove_booking_source_post')]
    operations = [migrations.AlterField(
        model_name='tbl_booking', name='zip_code',
        field=models.CharField(blank=True, null=True, default='', max_length=4,
            validators=[RegexValidator(r'^(0[4-9]\d{2}|[1-9]\d{3})$',
                'Must be a valid 4-digit Philippine Zip Code (0400 or higher).')]),
    )]
