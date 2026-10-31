from rest_framework import serializers
from .models import tbl_documents, tbl_audit_logs
from accounts.models import tbl_user_profile
from core.utils import get_signed_cloudinary_url


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
        return get_signed_cloudinary_url(instance.document_url, as_avatar=False)

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
    is_expired = serializers.SerializerMethodField()
    can_replace = serializers.SerializerMethodField()

    class Meta:
        model = tbl_documents
        fields = [
            'document_id',
            'document_type',
            'verification_status',
            'document_image_url',
            'date_issued',
            'valid_until',
            'is_expired',
            'can_replace',
            'extracted_data',
            'ocr_match_score',
            'ocr_discrepancies',
            'ocr_processed_at',
            'ocr_retry_count',
            'rejection_reason',
            'created_at',
        ]

    def get_document_image_url(self, instance):
        return get_signed_cloudinary_url(instance.document_url, as_avatar=False)

    def get_is_expired(self, instance):
        from accounts.workforce import document_is_expired
        return document_is_expired(instance)

    def get_can_replace(self, instance):
        from .services.document_upload import document_can_be_replaced
        return document_can_be_replaced(instance)


class AdminAuditLogSerializer(serializers.ModelSerializer):
    """
    Serializer for audit logs viewed by Superadmin.
    """
    class Meta:
        model = tbl_audit_logs
        fields = [
            'log_id',
            'actor',
            'actor_name',
            'actor_email',
            'actor_role',
            'actor_barangay',
            'target_user',
            'target_name',
            'target_email',
            'target_role',
            'target_barangay',
            'document',
            'document_id_snapshot',
            'document_type',
            'document_number',
            'action',
            'previous_status',
            'new_status',
            'reason',
            'ip_address',
            'user_agent',
            'metadata',
            'created_at',
        ]
