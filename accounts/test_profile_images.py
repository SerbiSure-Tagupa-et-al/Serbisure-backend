from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image
from rest_framework.exceptions import APIException
from rest_framework.test import APIRequestFactory, force_authenticate

from .models import tbl_user_profile
from .profile_images import save_profile_image
from .views import ProfileImageUploadView


class ProfileImageTests(SimpleTestCase):
    @patch('accounts.profile_images.cloudinary.uploader.upload')
    def test_authenticated_upload_only_saves_profile_field_and_returns_timings(self, upload):
        upload.return_value = {'public_id': 'serbisure_profiles/selfie'}
        user = SimpleNamespace(account_type='Admin', profile_link='old-image', save=Mock())
        timings = save_profile_image(user, 'image-file')
        upload.assert_called_once_with('image-file', folder='serbisure_profiles/', type='authenticated')
        user.save.assert_called_once_with(update_fields=['profile_link'])
        self.assertEqual(user.profile_link, 'serbisure_profiles/selfie')
        self.assertEqual(set(timings), {'cloudinary', 'profile_save'})
        self.assertTrue(all(value >= 0 for value in timings.values()))

    @patch('accounts.profile_images.cloudinary.uploader.upload', return_value={})
    def test_missing_cloudinary_confirmation_does_not_replace_existing_selfie(self, upload):
        user = SimpleNamespace(account_type='Admin', profile_link='old-image', save=Mock())
        with self.assertRaises(APIException):
            save_profile_image(user, 'image-file')
        user.save.assert_not_called()
        self.assertEqual(user.profile_link, 'old-image')

    @patch('accounts.serializers.get_signed_cloudinary_url', return_value='https://test.local/signed-selfie.jpg')
    @patch('accounts.profile_images.cloudinary.uploader.upload', return_value={'public_id': 'selfie'})
    def test_multipart_endpoint_returns_saved_image_and_cloudinary_timing(self, upload, signed_url):
        image = BytesIO()
        Image.new('RGB', (32, 32)).save(image, format='JPEG')
        file = SimpleUploadedFile('selfie.jpg', image.getvalue(), content_type='image/jpeg')
        user = tbl_user_profile(username='official', account_type='Admin')
        user.save = Mock()
        request = APIRequestFactory().put('/api/v1/accounts/profile-image/', {'profile_image': file}, format='multipart')
        force_authenticate(request, user=user)
        response = ProfileImageUploadView.as_view(throttle_classes=[])(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['profile_link'], 'https://test.local/signed-selfie.jpg')
        self.assertIn('cloudinary;dur=', response['Server-Timing'])
        self.assertIn('profile_save;dur=', response['Server-Timing'])
        self.assertIn('profile_request;dur=', response['Server-Timing'])
        user.save.assert_called_once_with(update_fields=['profile_link'])
