from rest_framework import serializers
from .models import tbl_documents
from accounts.models import tbl_user_profile
import cloudinary.utils


class UserProfileSnippetSerializer(serializers.ModelSerializer):
    class Meta:
        model = tbl_user_profile
        fields = [
            'id', 'email', 'first_name', 'middle_name', 'last_name',
            'contact_number', 'date_of_birth', 'account_type',
            'verification_status', 'date_joined'
        ]


class AdminDocumentDetailSerializer(serializers.ModelSerializer):
    user_profile = UserProfileSnippetSerializer(read_only=True)
    document_image_url = serializers.SerializerMethodField()
    verifyBy_email = serializers.SerializerMethodField()

    class Meta:
        model = tbl_documents
        fields = [
            'document_id',
            'document_type',
            'verification_status',
            'document_image_url',
            'date_issued',
            'valid_until',
            'extracted_data',
            'ocr_raw_text',
            'ocr_match_score',
            'ocr_discrepancies',
            'ocr_processed_at',
            'rejection_reason',
            'verifyBy',
            'verifyBy_email',
            'user_profile',
            'created_at',
        ]

    def get_document_image_url(self, instance):
        public_id = instance.document_url
        if not public_id:
            return None
        url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            type="authenticated",
            sign_url=True,
        )
        return url

    def get_verifyBy_email(self, instance):
        return instance.verifyBy.email if instance.verifyBy else None


class AdminDocumentActionSerializer(serializers.Serializer):
    verification_status = serializers.ChoiceField(choices=['Verified', 'Rejected'])
    rejection_reason = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate(self, data):
        if data.get('verification_status') == 'Rejected' and not data.get('rejection_reason'):
            raise serializers.ValidationError({"rejection_reason": "A reason must be provided when rejecting a document."})
        return data


class UserDocumentStatusSerializer(serializers.ModelSerializer):
    document_image_url = serializers.SerializerMethodField()

    class Meta:
        model = tbl_documents
        fields = [
            'document_id',
            'document_type',
            'verification_status',
            'document_image_url',
            'date_issued',
            'valid_until',
            'ocr_match_score',
            'ocr_discrepancies',
            'rejection_reason',
            'created_at',
        ]

    def get_document_image_url(self, instance):
        public_id = instance.document_url
        if not public_id:
            return None
        url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            type="authenticated",
            sign_url=True,
        )
        return url
