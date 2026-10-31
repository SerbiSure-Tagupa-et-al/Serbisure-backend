from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('accounts', '0024_penalty_adjustment')]
    operations = [
        migrations.RemoveField(model_name='tbl_user_profile', name='resume_url'),
        migrations.RemoveField(model_name='tbl_user_profile', name='resume_uploaded_at'),
    ]
