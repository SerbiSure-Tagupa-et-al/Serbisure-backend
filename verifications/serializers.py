from rest_framework import serializers
from accounts.workforce import PH_TIME
from .models import tbl_documents
from .services.document_upload import create_uploaded_document
import cloudinary.utils

class DocumentUploadSerializer(serializers.ModelSerializer):

    document_image = serializers.ImageField(
        write_only=True,
        required=True
    )

    class Meta:
        model = tbl_documents
        fields = [
            'document_id', 'document_type', 'date_issued', 
            'valid_until', 'document_image', 'document_url', 
            'verification_status', 'verifyBy', 'document_number',
            'extracted_data', 'ocr_extracted_data',
            'ocr_match_score', 'ocr_discrepancies', 'rejection_reason',
            'created_at'
        ]

        read_only_fields = [
            'document_url', 'verification_status', 'verifyBy',
            'valid_until', 'date_issued', 'document_number',
            'extracted_data', 'ocr_extracted_data',
            'ocr_match_score', 'ocr_discrepancies', 'rejection_reason',
            'created_at'
        ]

    def create(self, validated_data):
        image_file = validated_data.pop('document_image')
        return create_uploaded_document(self.context['request'].user, image_file, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        public_id = instance.document_url

        if public_id:
            if public_id.startswith('http://') or public_id.startswith('https://'):
                representation['document_url'] = public_id
            else:
                temporary_url, options = cloudinary.utils.cloudinary_url(
                    public_id,
                    type="authenticated",
                    sign_url=True,
                )
                representation['document_url'] = temporary_url

        return representation

    def validate_document_image(self, value):
        max_size = 10 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError("Image file must be under 10MB")
        return value


class AdminVerificationQueueSerializer(serializers.ModelSerializer):
    """
    Serializes verification queue items tailored for the Web Admin React dashboard.
    Matches the TypeScript VerificationRequest type.
    """
    id = serializers.CharField(source='document_id', read_only=True)
    userId = serializers.UUIDField(source='user_profile_id', read_only=True)
    submittedAt = serializers.DateTimeField(source='created_at', read_only=True)
    name = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    avatar = serializers.SerializerMethodField()
    documentType = serializers.SerializerMethodField()
    documentNumber = serializers.SerializerMethodField()
    submittedDate = serializers.SerializerMethodField()
    issuedDate = serializers.SerializerMethodField()
    validityDate = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    applicantStatus = serializers.SerializerMethodField()
    isExpired = serializers.SerializerMethodField()
    validUntil = serializers.DateField(source='valid_until', read_only=True)
    recordStatus = serializers.SerializerMethodField()
    documentImage = serializers.SerializerMethodField()
    documentImageBack = serializers.SerializerMethodField()
    rawDocumentType = serializers.CharField(source='document_type', read_only=True)
    barangay = serializers.SerializerMethodField()
    contactNumber = serializers.SerializerMethodField()
    email = serializers.SerializerMethodField()
    notes = serializers.CharField(source='rejection_reason', read_only=True)
    faceLivenessMatchScore = serializers.SerializerMethodField()
    ocrExtractedData = serializers.SerializerMethodField()
    ocrDiscrepancies = serializers.JSONField(source='ocr_discrepancies', read_only=True)
    ocrMatchScore = serializers.FloatField(source='ocr_match_score', read_only=True)

    # Package & Companion Document Fields
    primaryStatus = serializers.SerializerMethodField()
    isPackage = serializers.SerializerMethodField()
    packageLabel = serializers.SerializerMethodField()
    secondaryDocumentId = serializers.SerializerMethodField()
    secondaryDocumentImage = serializers.SerializerMethodField()
    secondaryDocumentType = serializers.SerializerMethodField()
    secondaryDocumentNumber = serializers.SerializerMethodField()
    secondaryIssuedDate = serializers.SerializerMethodField()
    secondaryValidityDate = serializers.SerializerMethodField()
    secondaryStatus = serializers.SerializerMethodField()
    secondaryNotes = serializers.SerializerMethodField()
    secondaryOcrData = serializers.SerializerMethodField()
    secondaryOcrDiscrepancies = serializers.SerializerMethodField()

    hasLguCoverage = serializers.SerializerMethodField()
    hasDocuments = serializers.SerializerMethodField()

    class Meta:
        model = tbl_documents
        fields = [
            'id', 'userId', 'submittedAt', 'name', 'role', 'avatar', 'documentType', 'rawDocumentType', 'documentNumber',
            'submittedDate', 'issuedDate', 'validityDate', 'status', 'primaryStatus', 'recordStatus',
            'applicantStatus', 'isExpired', 'validUntil',
            'documentImage', 'documentImageBack', 'barangay', 'contactNumber', 'email', 'notes',
            'faceLivenessMatchScore', 'ocrExtractedData', 'ocrDiscrepancies', 'ocrMatchScore',
            'isPackage', 'packageLabel', 'secondaryDocumentId', 'secondaryDocumentImage', 'secondaryDocumentType',
            'secondaryDocumentNumber', 'secondaryIssuedDate', 'secondaryValidityDate',
            'secondaryStatus', 'secondaryNotes', 'secondaryOcrData', 'secondaryOcrDiscrepancies',
            'hasLguCoverage', 'hasDocuments'
        ]

    def _get_companion_doc(self, obj):
        # Independent records: package metadata must not mask another document's status.
        return None

    def get_applicantStatus(self, obj):
        from accounts.workforce import credential_snapshot
        from .services.queue import PORTAL_STATUS
        statuses = self.context.get('applicant_statuses', {})
        if obj.user_profile_id in statuses:
            return statuses[obj.user_profile_id]
        return PORTAL_STATUS[credential_snapshot(obj.user_profile)[0]]

    def get_isExpired(self, obj):
        from accounts.workforce import document_is_expired
        return document_is_expired(obj)

    def get_isPackage(self, obj):
        companion = self._get_companion_doc(obj)
        return companion is not None

    def get_packageLabel(self, obj):
        companion = self._get_companion_doc(obj)
        if companion:
            if obj.document_type in ['nbi_clearance', 'police_clearance']:
                return 'Clearances (NBI + Police)'
            if 'national_id' in obj.document_type:
                return 'National ID (Front + Back)'
        return None

    def get_name(self, obj):
        u = obj.user_profile
        full = f"{u.first_name} {u.last_name}".strip()
        return full or u.username or "Registered User"

    def get_role(self, obj):
        acc_type = getattr(obj.user_profile, 'account_type', 'Homeowner')
        return acc_type.upper()

    def get_avatar(self, obj):
        profile_link = getattr(obj.user_profile, 'profile_link', None)
        if profile_link:
            if profile_link.startswith('http://') or profile_link.startswith('https://'):
                return profile_link
            try:
                temp_url, _ = cloudinary.utils.cloudinary_url(
                    profile_link,
                    type="authenticated",
                    sign_url=True,
                    format="webp",
                    quality="auto",
                )
                return temp_url
            except Exception:
                return profile_link
        # Default placeholder avatar
        return f"https://ui-avatars.com/api/?name={obj.user_profile.first_name}+{obj.user_profile.last_name}&background=F5A623&color=fff"

    def get_documentType(self, obj):
        companion = self._get_companion_doc(obj)
        if companion and obj.document_type in ['nbi_clearance', 'police_clearance']:
            return 'Clearances (NBI + Police)'
        if companion and obj.document_type in ['national_id_front', 'national_id_back']:
            return 'National ID (Front & Back)'
        mapping = {
            'nbi_clearance': 'NBI CLEARANCE',
            'police_clearance': 'Police Clearance',
            'national_id_front': 'National ID (Front)',
            'national_id_back': 'National ID (Back)',
        }
        return mapping.get(obj.document_type, obj.document_type.replace('_', ' ').title())

    def get_documentNumber(self, obj):
        if obj.document_number:
            return obj.document_number
        extracted = obj.ocr_extracted_data or {}
        val = (
            extracted.get('clearance_number') or
            extracted.get('philsys_number') or
            extracted.get('document_number')
        )
        if val:
            return val
        # If this is national_id_back, check front doc
        if obj.document_type == 'national_id_back':
            front = tbl_documents.objects.filter(user_profile=obj.user_profile, document_type='national_id_front').first()
            if front:
                f_ext = front.ocr_extracted_data or {}
                return front.document_number or f_ext.get('philsys_number') or f_ext.get('document_number') or "Not Detected"
        return "Not Detected"

    def get_submittedDate(self, obj):
        if obj.created_at:
            return obj.created_at.astimezone(PH_TIME).strftime('%b %d, %Y %I:%M %p PHT')
        return "Recent"

    def get_issuedDate(self, obj):
        if 'national_id' in obj.document_type:
            return 'N/A (PhilSys ID)'
        if obj.date_issued:
            return obj.date_issued.strftime('%b %d, %Y')
        extracted = obj.ocr_extracted_data or {}
        val = extracted.get('date_issued')
        if val:
            try:
                from datetime import datetime
                return datetime.strptime(str(val)[:10], '%Y-%m-%d').strftime('%b %d, %Y')
            except Exception:
                return str(val)
        return "Not Detected"

    def get_validityDate(self, obj):
        if 'national_id' in obj.document_type:
            return 'Permanent'
        if obj.valid_until:
            return obj.valid_until.strftime('%b %d, %Y')
        extracted = obj.ocr_extracted_data or {}
        val = extracted.get('valid_until')
        if val:
            try:
                from datetime import datetime
                return datetime.strptime(str(val)[:10], '%Y-%m-%d').strftime('%b %d, %Y')
            except Exception:
                return str(val)
        return "Not Detected"

    def get_status(self, obj):
        companion = self._get_companion_doc(obj)
        if companion:
            statuses = [obj.verification_status, companion.verification_status]
            if 'Rejected' in statuses:
                return 'REJECTED'
            elif all(s == 'Verified' for s in statuses):
                return 'VERIFIED'
            return 'PENDING / REVIEW'

        s = obj.verification_status
        if s == 'Verified':
            return 'VERIFIED'
        elif s == 'Rejected':
            return 'REJECTED'
        return 'PENDING / REVIEW'

    def get_recordStatus(self, obj):
        if self.get_status(obj) == 'VERIFIED':
            return 'Clear Record'
        companion = self._get_companion_doc(obj)
        has_critical = False
        if obj.ocr_discrepancies:
            has_critical = any(d.get('severity') == 'high' for d in obj.ocr_discrepancies)
        if not has_critical and companion and companion.ocr_discrepancies:
            has_critical = any(d.get('severity') == 'high' for d in companion.ocr_discrepancies)
        return 'Flagged' if has_critical else 'Under Review'

    def _sign_cloudinary_url(self, public_id):
        if not public_id:
            return None
        if public_id.startswith('http://') or public_id.startswith('https://'):
            return public_id
        try:
            temporary_url, _ = cloudinary.utils.cloudinary_url(
                public_id,
                type="authenticated",
                sign_url=True,
                format="webp",
                quality="auto",
            )
            return temporary_url
        except Exception:
            return public_id

    def get_documentImage(self, obj):
        # Primary image
        return self._sign_cloudinary_url(obj.document_url)

    def get_documentImageBack(self, obj):
        # Backward-compatible alias for secondary image
        return self.get_secondaryDocumentImage(obj)

    def get_secondaryDocumentImage(self, obj):
        companion = self._get_companion_doc(obj)
        if companion:
            return self._sign_cloudinary_url(companion.document_url)
        return None

    def get_secondaryDocumentType(self, obj):
        companion = self._get_companion_doc(obj)
        if companion:
            mapping = {
                'nbi_clearance': 'NBI CLEARANCE',
                'police_clearance': 'Police Clearance',
                'national_id_front': 'National ID (Front)',
                'national_id_back': 'National ID (Back)',
            }
            return mapping.get(companion.document_type, companion.document_type.replace('_', ' ').title())
        return None

    def get_secondaryDocumentNumber(self, obj):
        companion = self._get_companion_doc(obj)
        if not companion:
            return None
        if companion.document_number:
            return companion.document_number
        extracted = companion.ocr_extracted_data or {}
        val = (
            extracted.get('clearance_number') or
            extracted.get('philsys_number') or
            extracted.get('document_number')
        )
        return val or "Not Detected"

    def get_secondaryIssuedDate(self, obj):
        companion = self._get_companion_doc(obj)
        if not companion:
            return None
        if 'national_id' in companion.document_type:
            return 'N/A (PhilSys ID)'
        if companion.date_issued:
            return companion.date_issued.strftime('%b %d, %Y')
        extracted = companion.ocr_extracted_data or {}
        val = extracted.get('date_issued')
        if val:
            try:
                from datetime import datetime
                return datetime.strptime(str(val)[:10], '%Y-%m-%d').strftime('%b %d, %Y')
            except Exception:
                return str(val)
        return "Not Detected"

    def get_secondaryValidityDate(self, obj):
        companion = self._get_companion_doc(obj)
        if not companion:
            return None
        if 'national_id' in companion.document_type:
            return 'Permanent'
        if companion.valid_until:
            return companion.valid_until.strftime('%b %d, %Y')
        extracted = companion.ocr_extracted_data or {}
        val = extracted.get('valid_until')
        if val:
            try:
                from datetime import datetime
                return datetime.strptime(str(val)[:10], '%Y-%m-%d').strftime('%b %d, %Y')
            except Exception:
                return str(val)
        return "Not Detected"

    def get_primaryStatus(self, obj):
        s = obj.verification_status
        if s == 'Verified':
            return 'VERIFIED'
        elif s == 'Rejected':
            return 'REJECTED'
        return 'PENDING / REVIEW'

    def get_secondaryDocumentId(self, obj):
        companion = self._get_companion_doc(obj)
        if companion:
            return str(companion.document_id)
        return None

    def get_secondaryStatus(self, obj):
        companion = self._get_companion_doc(obj)
        if not companion:
            return None
        s = companion.verification_status
        if s == 'Verified':
            return 'VERIFIED'
        elif s == 'Rejected':
            return 'REJECTED'
        return 'PENDING / REVIEW'

    def get_secondaryNotes(self, obj):
        companion = self._get_companion_doc(obj)
        if companion:
            return companion.rejection_reason
        return None

    def get_secondaryOcrData(self, obj):
        companion = self._get_companion_doc(obj)
        if companion and companion.ocr_extracted_data:
            return dict(companion.ocr_extracted_data)
        return None

    def get_secondaryOcrDiscrepancies(self, obj):
        companion = self._get_companion_doc(obj)
        if companion and companion.ocr_discrepancies:
            return companion.ocr_discrepancies
        return []

    def get_ocrExtractedData(self, obj):
        data = dict(obj.ocr_extracted_data or {})
        if not data.get('philsys_number') and obj.document_number:
            data['philsys_number'] = obj.document_number
            data['document_number'] = obj.document_number
        if 'national_id' in obj.document_type:
            data['date_issued'] = None
        # If national_id_front, merge back's extracted data (blood_type, etc.)
        if obj.document_type == 'national_id_front':
            back_doc = self._get_companion_doc(obj)
            if back_doc and back_doc.ocr_extracted_data:
                for k, v in back_doc.ocr_extracted_data.items():
                    if k != 'date_issued' and v and not data.get(k):
                        data[k] = v
        return data

    def get_barangay(self, obj):
        if self.context.get('resident_scope'):
            from accounts.admin_statistics import barangay_name
            return barangay_name(obj.user_profile.barangay) or 'Unassigned'
        u = getattr(obj, 'user_profile', None)
        if not u:
            return 'Unassigned'
        brgy = (getattr(u, 'barangay', '') or '').strip()
        if brgy:
            return brgy
        street = (getattr(u, 'street', '') or '').lower()
        city = (getattr(u, 'city', '') or '').lower()
        from accounts.models import tbl_user_profile
        active_lgus = [
            b.strip() for b in tbl_user_profile.objects.filter(
                account_type='Barangay', is_active=True
            ).exclude(barangay__isnull=True).exclude(barangay__exact='')
            .values_list('barangay', flat=True) if b and b.strip()
        ]
        for b in active_lgus:
            if b.lower() in street or b.lower() in city:
                return b
        return 'Unassigned'

    def get_hasLguCoverage(self, obj):
        if 'active_lgus' in self.context:
            from accounts.admin_statistics import barangay_name
            return barangay_name(obj.user_profile.barangay).casefold() in self.context['active_lgus']
        brgy = self.get_barangay(obj)
        if not brgy or brgy.lower() == 'unassigned':
            return False
        from accounts.models import tbl_user_profile
        active_lgus = [
            b.strip().lower() for b in tbl_user_profile.objects.filter(
                account_type='Barangay', is_active=True
            ).exclude(barangay__isnull=True).exclude(barangay__exact='')
            .values_list('barangay', flat=True) if b and b.strip()
        ]
        return brgy.strip().lower() in active_lgus

    def get_hasDocuments(self, obj):
        return True

    def get_contactNumber(self, obj):
        return getattr(obj.user_profile, 'contact_number', '') or '+639171234567'

    def get_email(self, obj):
        return getattr(obj.user_profile, 'email', '')

    def get_faceLivenessMatchScore(self, obj):
        # No server-authoritative biometric measurement is currently persisted.
        return None
