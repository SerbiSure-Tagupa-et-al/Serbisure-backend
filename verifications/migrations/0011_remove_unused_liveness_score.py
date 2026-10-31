from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('verifications', '0010_document_processing_lease')]
    operations = [migrations.RemoveField(model_name='tbl_documents', name='face_liveness_score')]
