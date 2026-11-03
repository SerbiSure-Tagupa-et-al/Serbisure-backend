from django.db import migrations


def grandfather_photos(apps, schema_editor):
    User = apps.get_model('accounts', 'tbl_user_profile')
    Document = apps.get_model('verifications', 'tbl_documents')
    Audit = apps.get_model('verifications', 'tbl_audit_logs')
    db = schema_editor.connection.alias
    # Select only fields used here: unrelated account cleanup migrations can
    # already have removed legacy columns from the physical table.
    users = User.objects.using(db).filter(account_type__in=['Homeowner', 'Kasambahay']).exclude(profile_link__isnull=True).exclude(profile_link='').only(
        'id', 'profile_link', 'first_name', 'last_name', 'email', 'account_type', 'barangay')
    for user in users.iterator(chunk_size=500):
        if not user.profile_link.strip() or Document.objects.using(db).filter(user_profile_id=user.pk, document_type='profile_photo').exists():
            continue
        document = Document.objects.using(db).create(user_profile_id=user.pk, document_type='profile_photo',
                                                    document_url=user.profile_link, verification_status='Verified')
        Audit.objects.using(db).create(document_id=document.pk, document_id_snapshot=document.pk,
            document_type='profile_photo', target_user_id=user.pk,
            target_name=f'{user.first_name} {user.last_name}'.strip(), target_email=user.email,
            target_role=user.account_type, target_barangay=user.barangay,
            actor_name='System', actor_role='System', action='APPROVED', new_status='Verified',
            reason='Existing profile photo kept verified when photo review was introduced.')


class Migration(migrations.Migration):
    dependencies = [('verifications', '0012_profile_photo_type'), ('accounts', '0014_add_region_barangay_to_user_profile')]
    # Preserve review history on rollback; the preceding constraint must not be
    # reversed until profile-photo records have been handled deliberately.
    operations = [migrations.RunPython(grandfather_photos, migrations.RunPython.noop)]
