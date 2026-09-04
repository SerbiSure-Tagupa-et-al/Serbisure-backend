from unittest.mock import patch, MagicMock
from io import BytesIO
from PIL import Image
from django.urls import reverse
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import tbl_user_profile
from verifications.models import tbl_documents
from verifications.services.matching_service import (
    calculate_similarity,
    detect_discrepancies,
)
from notifications.models import tbl_notification


class MatchingServiceUnitTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.user = tbl_user_profile.objects.create_user(
            username="anabeal_test",
            email="anabeal@test.com",
            password="password123",
            first_name="Anabeal",
            middle_name="Santos",
            last_name="Reyes",
            account_type="Kasambahay",
        )

    def test_exact_name_similarity(self):
        sim = calculate_similarity("Anabeal", "ANABEAL")
        self.assertEqual(sim, 1.0)

    def test_high_similarity_fuzzy(self):
        sim = calculate_similarity("Anabeal", "Anabel")
        self.assertGreaterEqual(sim, 0.85)

    def test_discrepancy_detected_for_unrelated_name(self):
        # User requested: "Anabeal" vs "Scaryyy"
        extracted_data = {
            "first_name": "Scaryyy",
            "last_name": "Reyes",
            "valid_until": "2029-12-31",
        }
        score, discrepancies = detect_discrepancies(extracted_data, self.user)
        self.assertLess(score, 0.70)
        self.assertTrue(any(d["field"] == "first_name" for d in discrepancies))
        first_name_disc = next(d for d in discrepancies if d["field"] == "first_name")
        self.assertEqual(first_name_disc["profile_value"], "Anabeal")
        self.assertEqual(first_name_disc["document_value"], "Scaryyy")
        self.assertEqual(first_name_disc["severity"], "high")

    def test_expired_document_detection(self):
        extracted_data = {
            "first_name": "Anabeal",
            "last_name": "Reyes",
            "valid_until": "2020-01-01",  # Past date
        }
        score, discrepancies = detect_discrepancies(extracted_data, self.user)
        self.assertTrue(any(d["field"] == "valid_until" for d in discrepancies))
        exp_disc = next(d for d in discrepancies if d["field"] == "valid_until")
        self.assertEqual(exp_disc["severity"], "critical")


class VerificationWorkflowTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.kasambahay = tbl_user_profile.objects.create_user(
            username="kasa_worker",
            email="kasa_worker@test.com",
            password="password123",
            first_name="Maria",
            last_name="Santos",
            account_type="Kasambahay",
        )

        self.admin = tbl_user_profile.objects.create_user(
            username="admin_user",
            email="admin@serbisure.com",
            password="password123",
            first_name="Admin",
            last_name="Official",
            account_type="Admin",
            is_staff=True,
        )

        self.barangay = tbl_user_profile.objects.create_user(
            username="brgy_officer",
            email="brgy@serbisure.com",
            password="password123",
            first_name="Barangay",
            last_name="Officer",
            account_type="Barangay",
        )

        self.upload_url = reverse("document-upload")
        self.status_url = reverse("document-status")
        self.admin_list_url = reverse("admin-document-list")

    def generate_dummy_image(self):
        file = BytesIO()
        image = Image.new("RGB", (20, 20), "white")
        image.save(file, "jpeg")
        file.name = "document.jpg"
        file.seek(0)
        return SimpleUploadedFile(file.name, file.read(), content_type="image/jpeg")

    @patch("cloudinary.uploader.upload")
    def test_upload_and_status_flow(self, mock_cloudinary):
        mock_cloudinary.return_value = {"public_id": "test_public_id_nbi"}
        self.client.force_authenticate(user=self.kasambahay)

        data = {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
        }
        res = self.client.post(self.upload_url, data, format="multipart")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["verification_status"], "Pending")

        # Check user status endpoint
        status_res = self.client.get(self.status_url)
        self.assertEqual(status_res.status_code, status.HTTP_200_OK)
        self.assertEqual(status_res.data["overall_status"], "Pending")
        self.assertEqual(len(status_res.data["documents"]), 1)
        self.assertEqual(status_res.data["documents"][0]["verification_status"], "Pending")

    @patch("cloudinary.uploader.upload")
    def test_reupload_rejected_document(self, mock_cloudinary):
        mock_cloudinary.return_value = {"public_id": "test_public_id_nbi"}
        self.client.force_authenticate(user=self.kasambahay)

        # 1. Initial upload
        data = {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
        }
        res1 = self.client.post(self.upload_url, data, format="multipart")
        self.assertEqual(res1.status_code, status.HTTP_201_CREATED)
        doc_id = res1.data["document_id"]

        # 2. Admin rejects the document
        self.client.force_authenticate(user=self.admin)
        action_url = reverse("admin-document-action", kwargs={"document_id": doc_id})
        reject_res = self.client.patch(action_url, {
            "verification_status": "Rejected",
            "rejection_reason": "Image is blurry. Please re-upload.",
        })
        self.assertEqual(reject_res.status_code, status.HTTP_200_OK)

        # 3. Kasambahay re-uploads the rejected document -> MUST SUCCEED!
        self.client.force_authenticate(user=self.kasambahay)
        mock_cloudinary.return_value = {"public_id": "test_public_id_nbi_new"}
        res2 = self.client.post(self.upload_url, {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
        }, format="multipart")
        self.assertEqual(res2.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res2.data["verification_status"], "Pending")

        # 4. Re-uploading again while Pending must hit 409 Conflict
        res3 = self.client.post(self.upload_url, {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
        }, format="multipart")
        self.assertEqual(res3.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("already submitted", str(res3.data))

    @patch("cloudinary.uploader.upload")
    def test_delete_rejected_document(self, mock_cloudinary):
        mock_cloudinary.return_value = {"public_id": "test_public_id_police"}
        self.client.force_authenticate(user=self.kasambahay)

        res = self.client.post(self.upload_url, {
            "document_type": "police_clearance",
            "document_image": self.generate_dummy_image(),
        }, format="multipart")
        doc_id = res.data["document_id"]
        delete_url = reverse("document-delete-rejected", kwargs={"document_id": doc_id})

        # Cannot delete while Pending
        del_fail = self.client.delete(delete_url)
        self.assertEqual(del_fail.status_code, status.HTTP_400_BAD_REQUEST)

        # Admin rejects
        self.client.force_authenticate(user=self.barangay)
        action_url = reverse("admin-document-action", kwargs={"document_id": doc_id})
        self.client.patch(action_url, {
            "verification_status": "Rejected",
            "rejection_reason": "Invalid document copy.",
        })

        # Kasambahay can now delete the rejected document
        self.client.force_authenticate(user=self.kasambahay)
        del_ok = self.client.delete(delete_url)
        self.assertEqual(del_ok.status_code, status.HTTP_200_OK)
        self.assertFalse(tbl_documents.objects.filter(document_id=doc_id).exists())

    @patch("cloudinary.uploader.upload")
    def test_admin_and_barangay_approval_flow(self, mock_cloudinary):
        mock_cloudinary.return_value = {"public_id": "test_public_id_nbi"}
        self.client.force_authenticate(user=self.kasambahay)

        res = self.client.post(self.upload_url, {
            "document_type": "nbi_clearance",
            "document_image": self.generate_dummy_image(),
        }, format="multipart")
        doc_id = res.data["document_id"]

        # Kasambahay cannot access admin document list
        admin_list_fail = self.client.get(self.admin_list_url)
        self.assertEqual(admin_list_fail.status_code, status.HTTP_403_FORBIDDEN)

        # Barangay official approves document
        self.client.force_authenticate(user=self.barangay)
        admin_list_ok = self.client.get(self.admin_list_url)
        self.assertEqual(admin_list_ok.status_code, status.HTTP_200_OK)

        action_url = reverse("admin-document-action", kwargs={"document_id": doc_id})
        approve_res = self.client.patch(action_url, {
            "verification_status": "Verified",
        })
        self.assertEqual(approve_res.status_code, status.HTTP_200_OK)

        # Verify document is verified and verifyBy is the barangay officer
        doc = tbl_documents.objects.get(document_id=doc_id)
        self.assertEqual(doc.verification_status, "Verified")
        self.assertEqual(doc.verifyBy, self.barangay)

        # Verify user profile is now Verified
        self.kasambahay.refresh_from_db()
        self.assertEqual(self.kasambahay.verification_status, "Verified")

        # Verify notification was sent
        notif = tbl_notification.objects.filter(receiver_id=self.kasambahay).order_by("-createdAt").first()
        self.assertIsNotNone(notif)
        self.assertIn("verified", notif.notification_message.lower())


class GoogleVisionOcrUnitTests(APITestCase):
    @patch.dict("os.environ", {"GOOGLE_VISION_API_KEY": "test-vision-key"})
    def test_is_google_vision_available_true(self):
        from verifications.services.ocr_service import is_google_vision_available
        self.assertTrue(is_google_vision_available())

    @patch.dict("os.environ", {}, clear=True)
    def test_is_google_vision_available_false(self):
        from verifications.services.ocr_service import is_google_vision_available
        self.assertFalse(is_google_vision_available())

    @patch.dict("os.environ", {}, clear=True)
    def test_extract_text_missing_key_raises_value_error(self):
        from verifications.services.ocr_service import extract_text_with_google_vision
        with self.assertRaises(ValueError):
            extract_text_with_google_vision(b"fake_image_bytes")

    @patch.dict("os.environ", {"GOOGLE_VISION_API_KEY": "test-vision-key"})
    @patch("verifications.services.ocr_service.requests.post")
    def test_extract_text_with_full_text_annotation(self, mock_post):
        from verifications.services.ocr_service import extract_text_with_google_vision
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "responses": [
                {
                    "fullTextAnnotation": {
                        "text": "REPUBLIC OF THE PHILIPPINES\nNATIONAL BUREAU OF INVESTIGATION\nNAME: ANABEAL REYES"
                    }
                }
            ]
        }
        mock_post.return_value = mock_response

        text = extract_text_with_google_vision(b"fake_bytes")
        self.assertIn("NATIONAL BUREAU OF INVESTIGATION", text)
        self.assertIn("ANABEAL REYES", text)
        mock_post.assert_called_once()
        self.assertIn("key=test-vision-key", mock_post.call_args[0][0])

    @patch.dict("os.environ", {"GOOGLE_VISION_API_KEY": "test-vision-key"})
    @patch("verifications.services.ocr_service.requests.post")
    def test_extract_text_fallback_to_text_annotations(self, mock_post):
        from verifications.services.ocr_service import extract_text_with_google_vision
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "responses": [
                {
                    "textAnnotations": [
                        {"description": "PHILIPPINE NATIONAL POLICE CLEARANCE CERTIFICATE"}
                    ]
                }
            ]
        }
        mock_post.return_value = mock_response

        text = extract_text_with_google_vision(b"fake_bytes")
        self.assertEqual(text, "PHILIPPINE NATIONAL POLICE CLEARANCE CERTIFICATE")

    @patch.dict("os.environ", {"GOOGLE_VISION_API_KEY": "test-vision-key"})
    @patch("verifications.services.ocr_service.requests.post")
    def test_extract_text_api_error_raises_exception(self, mock_post):
        import requests
        from verifications.services.ocr_service import extract_text_with_google_vision
        mock_response = MagicMock()
        mock_response.ok = False
        mock_response.status_code = 403
        mock_response.text = "Permission denied"
        mock_response.raise_for_status.side_effect = requests.HTTPError("403 Client Error")
        mock_post.return_value = mock_response

        with self.assertRaises(requests.HTTPError):
            extract_text_with_google_vision(b"fake_bytes")


class DocumentProcessorPipelineTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.user = tbl_user_profile.objects.create_user(
            username="processor_test_user",
            email="processor@test.com",
            password="password123",
            first_name="Anabeal",
            middle_name="Santos",
            last_name="Reyes",
            account_type="Kasambahay",
        )
        self.doc = tbl_documents.objects.create(
            user_profile=self.user,
            document_type="nbi_clearance",
            document_url="sample_public_id_nbi",
            verification_status="Pending",
        )

    @patch("verifications.services.document_processor.fetch_image_from_cloudinary")
    @patch("verifications.services.document_processor.is_google_vision_available")
    @patch("verifications.services.document_processor.extract_text_with_google_vision")
    @patch("verifications.services.document_processor.extract_structured_data_from_text")
    def test_full_pipeline_with_google_vision_and_groq(
        self,
        mock_groq,
        mock_vision,
        mock_is_vision_available,
        mock_fetch,
    ):
        from verifications.services.document_processor import process_document
        dummy_img = Image.new("RGB", (20, 20), "white")
        mock_fetch.return_value = (dummy_img, b"fake_bytes")
        mock_is_vision_available.return_value = True
        mock_vision.return_value = "REPUBLIC OF THE PHILIPPINES NBI CLEARANCE ANABEAL REYES VALID UNTIL 2028-12-31"
        mock_groq.return_value = {
            "document_type": "NBI Clearance",
            "first_name": "Anabeal",
            "middle_name": "Santos",
            "last_name": "Reyes",
            "date_issued": "2024-01-01",
            "valid_until": "2028-12-31",
            "id_number": "NBI-2024-123456",
        }

        process_document(self.doc.document_id)

        self.doc.refresh_from_db()
        self.assertIn("NBI CLEARANCE", self.doc.ocr_raw_text)
        self.assertEqual(self.doc.extracted_data["first_name"], "Anabeal")
        self.assertGreaterEqual(self.doc.ocr_match_score, 0.90)
        self.assertEqual(len(self.doc.ocr_discrepancies), 0)
        self.assertIsNotNone(self.doc.ocr_processed_at)
        # CRITICAL: Always remains Pending for manual Admin/Barangay review!
        self.assertEqual(self.doc.verification_status, "Pending")

        # Check notification sent
        notif = tbl_notification.objects.filter(receiver_id=self.user).first()
        self.assertIsNotNone(notif)
        self.assertIn("under review", notif.notification_message)

