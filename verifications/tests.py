from rest_framework.test import APITestCase
from rest_framework import status
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from accounts.models import tbl_user_profile
from unittest.mock import patch, PropertyMock
from io import BytesIO
from PIL import Image

class DocumentUploadTests(APITestCase):

    def setUp(self):
        ocr_patch = patch('verifications.ocr_service.process_document_ocr', return_value={
            'raw_text': '', 'extracted_data': {}, 'match_score': None, 'discrepancies': [],
        })
        ocr_patch.start()
        self.addCleanup(ocr_patch.stop)
        worker_patch = patch('verifications.services.document_processor.process_document_async')
        worker_patch.start()
        self.addCleanup(worker_patch.stop)

        super().setUp()
        # 1. Create a Kasambahay User
        self.kasambahay = tbl_user_profile.objects.create_user(
            username="kasa_test",
            email="kasa@test.com",
            password="password123",
            first_name="Kasa",
            last_name="Test",
            account_type="Kasambahay"
        )
        
        # 2. Create a Homeowner User
        self.homeowner = tbl_user_profile.objects.create_user(
            username="home_test",
            email="home@test.com",
            password="password123",
            first_name="Home",
            last_name="Test",
            account_type="Homeowner"
        )
        
        self.upload_url = reverse('document-upload') # Ensure your verifications/urls.py names it 'document-upload'

    def generate_dummy_image(self):
        """Creates a tiny real image in memory to pass Pillow's strict ImageField checks"""
        file = BytesIO()
        image = Image.new('RGB', (10, 10), 'white')
        image.save(file, 'jpeg')
        file.name = 'test_image.jpg'
        file.seek(0)
        return SimpleUploadedFile(file.name, file.read(), content_type='image/jpeg')

    # --- 1. UNAUTHENTICATED TEST ---
    def test_unauthenticated_user_cannot_upload(self):
        data = {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        response = self.client.post(self.upload_url, data, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # --- 2. BUSINESS LOGIC & IDIOT INPUT TESTS ---
    def test_kasambahay_cannot_upload_national_id(self):
        self.client.force_authenticate(user=self.kasambahay)
        data = {
            "document_type": "national_id_front",
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        response = self.client.post(self.upload_url, data, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Kasambahay can only upload NBI or Police Clearances", str(response.data))

    def test_homeowner_cannot_upload_nbi(self):
        self.client.force_authenticate(user=self.homeowner)
        data = {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        response = self.client.post(self.upload_url, data, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Homeowner can only upload a National ID", str(response.data))

    def test_invalid_document_type(self):
        self.client.force_authenticate(user=self.kasambahay)
        data = {
            "document_type": "fake_document_type", # Idiot input
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        response = self.client.post(self.upload_url, data, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_upload_pdf_instead_of_image(self):
        self.client.force_authenticate(user=self.kasambahay)
        pdf_file = SimpleUploadedFile("virus.pdf", b"fake_pdf_content", content_type='application/pdf')
        data = {
            "document_type": "nbi_clearance",
            "document_image": pdf_file, # Pillow should instantly block this!
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        response = self.client.post(self.upload_url, data, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- 3. SIZE VALIDATION TEST ---
    @patch('django.core.files.uploadedfile.InMemoryUploadedFile.size', new_callable=PropertyMock)
    def test_file_size_exceeds_10mb(self, mock_size):
        # We mock the size to be 11MB (11 * 1024 * 1024 bytes) without actually creating a massive file!
        mock_size.return_value = 11534336 
        
        self.client.force_authenticate(user=self.kasambahay)
        data = {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        response = self.client.post(self.upload_url, data, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Image file must be under 10MB", str(response.data))

    # --- 4. CLOUDINARY MOCK & SUCCESSFUL UPLOAD ---
    @patch('cloudinary.uploader.upload')
    def test_kasambahay_valid_upload_nbi(self, mock_cloudinary):
        # Prevent actually uploading to Cloudinary (saves your free tier limits!)
        mock_cloudinary.return_value = {'public_id': 'fake_cloudinary_url_123'}

        self.client.force_authenticate(user=self.kasambahay)
        data = {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        response = self.client.post(self.upload_url, data, format='multipart')
        
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mock_cloudinary.assert_called_once() # Verify Cloudinary was actually triggered

    # --- 5. IDEMPOTENCY TESTS ---
    @patch('cloudinary.uploader.upload')
    def test_kasambahay_idempotency_duplicate_nbi(self, mock_cloudinary):
        mock_cloudinary.return_value = {'public_id': 'fake_cloudinary_url_123'}
        self.client.force_authenticate(user=self.kasambahay)
        
        data = {
            "document_type": "nbi_clearance", 
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        
        # 1st Upload (Success)
        res1 = self.client.post(self.upload_url, data, format='multipart')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        
        # 2nd Upload exactly the same document (Should fail)
        data2 = {
            "document_type": "nbi_clearance", 
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }
        res2 = self.client.post(self.upload_url, data2, format='multipart')
        self.assertEqual(res2.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("already submitted", str(res2.data))

    @patch('cloudinary.uploader.upload')
    def test_kasambahay_can_upload_two_different_documents(self, mock_cloudinary):
        mock_cloudinary.return_value = {'public_id': 'fake_cloudinary_url_123'}
        self.client.force_authenticate(user=self.kasambahay)
        
        # 1. Upload NBI
        res1 = self.client.post(self.upload_url, {
            "document_type": "nbi_clearance", 
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }, format='multipart')
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        
        # 2. Upload Police Clearance (Should Succeed!)
        res2 = self.client.post(self.upload_url, {
            "document_type": "police_clearance", 
            "document_image": self.generate_dummy_image(),
            "date_issued": "2024-01-01",
            "valid_until": "2025-01-01"
        }, format='multipart')
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)

    # --- 6. RATE LIMITING TEST ---
    def test_rate_limiting_blocks_spam(self):
        self.client.force_authenticate(user=self.homeowner)
        
        # The configured limit is 20 per day. We will send 21 requests!
        # Note: Even if a request fails validation, DRF still counts it towards the throttle!
        for i in range(20):
            res = self.client.post(self.upload_url, {}, format='multipart')
            self.assertNotEqual(res.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
            
        # The 21st request should be throttled!
        spam_res = self.client.post(self.upload_url, {}, format='multipart')
        self.assertEqual(spam_res.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertIn("Too many attempts", str(spam_res.data))
