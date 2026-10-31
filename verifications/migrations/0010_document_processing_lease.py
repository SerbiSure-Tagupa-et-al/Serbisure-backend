from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('verifications', '0009_normalize_document_scores')]

    operations = [
        migrations.AddField(
            model_name='tbl_documents', name='ocr_processing_token',
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='tbl_documents', name='ocr_last_attempt_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]
