"""Profile image persistence and upload timing, without logging image or user data."""
from time import perf_counter

import cloudinary.uploader
from rest_framework.exceptions import APIException


def save_profile_image(user, image_file):
    from verifications.services.profile_photos import check_photo_upload, save_photo_submission
    check_photo_upload(user)
    started_at = perf_counter()
    result = cloudinary.uploader.upload(
        image_file,
        folder='serbisure_profiles/',
        type='authenticated',
    )
    cloudinary_ms = (perf_counter() - started_at) * 1000
    public_id = result.get('public_id')
    if not public_id:
        raise APIException('Your selfie could not be saved. Please retry.')

    started_at = perf_counter()
    if user.account_type in ('Homeowner', 'Kasambahay'):
        save_photo_submission(user, public_id)
    else:
        user.profile_link = public_id
        user.save(update_fields=['profile_link'])
    return {
        'cloudinary': cloudinary_ms,
        'profile_save': (perf_counter() - started_at) * 1000,
    }
