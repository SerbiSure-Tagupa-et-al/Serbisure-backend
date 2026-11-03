"""Explicit complete-identity fixtures for tests that require verified accounts."""
from accounts.models import tbl_user_profile
from verifications.models import tbl_documents


def create_verified_test_user(*args, **kwargs):
    # Preserve the existing manager's identity-document fixtures, and add the
    # newly required reviewed face photo in tests, never in production factories.
    user = tbl_user_profile.objects.create_user(*args, **kwargs)
    if kwargs.get('verification_status') == 'Verified' and user.account_type in ('Homeowner', 'Kasambahay'):
        tbl_documents.objects.create(user_profile=user, document_type='profile_photo',
            document_url=user.profile_link or 'https://example.test/approved-face.jpg', verification_status='Verified')
    return user
