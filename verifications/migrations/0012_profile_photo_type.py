from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('verifications', '0011_remove_unused_liveness_score')]
    operations = [
        migrations.RemoveConstraint(model_name='tbl_documents', name='valid_document_type_enum'),
        migrations.AlterField(model_name='tbl_documents', name='document_type', field=models.CharField(
            max_length=100, choices=[('nbi_clearance', 'NBI Clearance'), ('police_clearance', 'Police Clearance'),
                                    ('national_id_front', 'National ID (Front)'), ('national_id_back', 'National ID (Back)'),
                                    ('profile_photo', 'Profile Photo')])),
        migrations.AddConstraint(model_name='tbl_documents', constraint=models.CheckConstraint(
            condition=models.Q(document_type__in=['nbi_clearance', 'police_clearance', 'national_id_front', 'national_id_back', 'profile_photo']),
            name='valid_document_type_enum')),
    ]
