from rest_framework import serializers
from django.db import IntegrityError
from django.db.models import Q
from .models import tbl_user_profile
from datetime import date
from core.utils import (
    convert_title,
    check_input_letters,
    normalize_ph_phone_number,
    get_signed_cloudinary_url
)
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework.exceptions import AuthenticationFailed, ValidationError
import uuid
import re
from urllib.parse import urlparse
from django.db.models import Q

class UserRegistrationSerializer(serializers.ModelSerializer):
    # This enrsure the password is required to create an account,
    # but the API will never will accidentally send it back to the frontend

    password = serializers.CharField(write_only=True, trim_whitespace=False)
    verification_status = serializers.CharField(read_only=True)
    contact_number = serializers.CharField(max_length=25, required=True)
    user_about = serializers.CharField(max_length=500, required=False, allow_blank=True)
    user_tags = serializers.ListField(child=serializers.CharField(max_length=25), required=False)
    region = serializers.CharField(max_length=100, required=False, allow_blank=True)
    barangay = serializers.CharField(max_length=100, required=False, allow_blank=True)
    street = serializers.CharField(max_length=100, required=False, allow_blank=True)

    class Meta: 
        model = tbl_user_profile

        # These are the fields the user is allowed to submit when registering
        
        fields = [
            'first_name',
            'middle_name',
            'last_name',
            'date_of_birth',
            'religion',
            'nationality',
            'region',
            'province',
            'city',
            'barangay',
            'street',
            'zipcode',
            'country',
            'gender',
            'contact_number',
            'language',
            'email',
            'password',
            'account_type',
            'verification_status', 
            'user_about',
            'user_tags',
        ]
        extra_kwargs = {
            'email': {
                'error_messages': {
                    'unique': 'A user with this email address is already registered.'
                }
            },
            'contact_number': {
                'error_messages': {
                    'unique': 'A user with this contact number is already registered.'
                }
            }
        }

    # We override the standard save method to ensure the password gets hashed
    def create(self, validated_data):
        
        # 1. Take the password out of the data so we can securely hash it
        password = validated_data.pop('password')

        first_name = validated_data.get('first_name', '')
        middle_name = validated_data.get('middle_name', '')
        last_name = validated_data.get('last_name', '')

        combined = f"{first_name}{middle_name}{last_name}".replace(" ", "").lower()
        random_suffix = str(uuid.uuid4())[:5]

        validated_data['username'] = f"{combined}_{random_suffix}"

        try:
            user = tbl_user_profile(**validated_data)
            user.set_password(password)
            user.save()
            return user
        except IntegrityError as e:
            err_str = str(e).lower()
            if 'contact_number' in err_str:
                raise serializers.ValidationError({"contact_number": ["A user with this contact number is already registered."]})
            elif 'email' in err_str:
                raise serializers.ValidationError({"email": ["A user with this email address is already registered."]})
            raise serializers.ValidationError({"detail": "An account with these details already exists."})
    
    # Mandatory Value in creating a account
    # def mandatory_field_account_creation(self, value):


    # Custom Validation: Check if they are 18+
    def validate_date_of_birth(self, value):
        
        if value:
            today = date.today()
        
            # This handle leap year and birthday math automatically
            age = today.year - value.year - (( today.month, today.day) < (value.month, value.day))

            if age < 18:
                
                raise serializers.ValidationError("Minimum age is 18")

        return value

    def validate_password(self, value):

        # 1. Reject leading/trailing whitespace (prevents mobile autocomplete auto-space lockout)
        if value != value.strip():
            raise serializers.ValidationError("Password cannot start or end with spaces.")

        # 2. Check length (Minimum 8 characters for non-techy accessibility)
        if len(value) < 8: 
            raise serializers.ValidationError("Password must be at least 8 characters long.")

        if len(value) > 30:
            raise serializers.ValidationError("Password cannot exceed 30 characters.")

        # 3. Check for at least one number 
        if not any(char.isdigit() for char in value):
            raise serializers.ValidationError("Password must contain at least one number.")

        # 4. Check for at least one letter 
        if not any(char.isalpha() for char in value):
            raise serializers.ValidationError("Password must contain at least one letter.")
        
        # 5. Null byte check
        if '\x00' in value:
            raise serializers.ValidationError("Password cannot contain null characters.")

        return value         

    def validate_first_name(self, text):
        
        if not check_input_letters(text):
            raise serializers.ValidationError("First name must be 3-50 characters long and contain only letters and spaces.")
        
        return convert_title(text)
    
    def validate_middle_name(self, text):
        
        if not check_input_letters(text):
            raise serializers.ValidationError("Middle name must be 3-50 characters long and contain only letters and spaces.")
        
        return convert_title(text)
    
    def validate_last_name(self, text):
        
        if not check_input_letters(text):
            raise serializers.ValidationError("Last name must be 3-50 characters long and contain only letters and spaces.")
        
        return convert_title(text)

    def validate_account_type(self, value):
        if value == "Admin":
            raise serializers.ValidationError("You cannot create an Admin account through this public endpoints")
        return value
    
    def validate_email(self, value):
        val = value.strip().lower()
        if tbl_user_profile.objects.filter(email__iexact=val).exists():
            raise serializers.ValidationError("A user with this email address is already registered.")
        return val

    def validate_contact_number(self, value):
        normalized = normalize_ph_phone_number(value)
        if not normalized:
            raise serializers.ValidationError("Phone number must start with '+63' followed by 10 digits (e.g., +639123456789 or 09123456789).")
        
        if tbl_user_profile.objects.filter(contact_number=normalized).exists():
            raise serializers.ValidationError("A user with this contact number is already registered.")

        return normalized
    
    def validate_region(self, value):
        if not value:
            return value
        value = value.strip()
        if not value:
            return ""
        if len(value) < 2 or len(value) > 100:
            raise serializers.ValidationError("Region must be between 2 and 100 characters.")
        if not re.match(r"^[\w\s\-().,']+$", value, re.UNICODE):
            raise serializers.ValidationError("Region contains invalid characters.")
        return value.title()

    def validate_barangay(self, value):
        if not value:
            return value
        value = value.strip()
        if not value:
            return ""
        if len(value) < 2 or len(value) > 100:
            raise serializers.ValidationError("Barangay must be between 2 and 100 characters.")
        if not re.match(r"^[\w\s\-().,']+$", value, re.UNICODE):
            raise serializers.ValidationError("Barangay contains invalid characters.")
        return value.title()

    def validate_street(self, value):
        if not value:
            return value
        value = value.strip()
        if not value:
            return ""
        blocked_patterns = [r'\bbrgy\b', r'\bbarangay\b', r'\bbgy\b']
        for pattern in blocked_patterns:
            if re.search(pattern, value, re.IGNORECASE):
                raise serializers.ValidationError(
                    "Please use the 'Barangay' field for your barangay. "
                    "The Street field should only contain your House/Lot/Block number and street name."
                )
        if len(value) > 100:
            raise serializers.ValidationError("Street address cannot exceed 100 characters.")
        return value

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
    
        token['first_name'] = user.first_name
        token['middle_name'] = user.middle_name
        token['last_name'] = user.last_name
        token['account_type'] = user.account_type
        token['verification_status'] = user.verification_status
        token['language'] = user.language
        token['email'] = user.email
        token['contact_number'] = user.contact_number
        token['show_contact_number'] = getattr(user, 'show_contact_number', True)
        token['cancellation_strikes'] = getattr(user, 'cancellation_strikes', 0)
        token['is_restricted'] = getattr(user, 'is_restricted', False)
        token['user_tags'] = user.user_tags or []
        token['region'] = user.region or ''
        token['province'] = user.province or ''
        token['city'] = user.city or ''
        token['barangay'] = user.barangay or ''
        token['street'] = user.street or ''
        token['zipcode'] = user.zipcode or ''
        token['country'] = user.country or 'Philippines'
        token['date_of_birth'] = str(user.date_of_birth) if user.date_of_birth else ''
        token['gender'] = user.gender or ''
        token['nationality'] = user.nationality or 'Filipino'
        token['religion'] = user.religion or ''

        token['profile_link'] = get_signed_cloudinary_url(user.profile_link, as_avatar=True)
        return token
    

class CustomLoginSerializer(TokenObtainPairSerializer):
    default_error_messages = {
        "no_active_account": "Wrong email or password. Please try again!"
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'] = serializers.CharField(required=False, write_only=True)
        self.fields['identifier'] = serializers.CharField(required=False, write_only=True)
        self.fields['contact_number'] = serializers.CharField(required=False, write_only=True)

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
    
        token['first_name'] = user.first_name
        token['middle_name'] = user.middle_name
        token['last_name'] = user.last_name
        token['account_type'] = user.account_type
        token['verification_status'] = user.verification_status
        token['language'] = user.language
        token['email'] = user.email
        token['contact_number'] = user.contact_number
        token['show_contact_number'] = getattr(user, 'show_contact_number', True)
        token['cancellation_strikes'] = getattr(user, 'cancellation_strikes', 0)
        token['is_restricted'] = getattr(user, 'is_restricted', False)
        token['social_links'] = getattr(user, 'social_links', []) or []
        token['show_social_links'] = getattr(user, 'show_social_links', True)
        token['user_tags'] = user.user_tags or []
        token['region'] = user.region or ''
        token['province'] = user.province or ''
        token['city'] = user.city or ''
        token['barangay'] = user.barangay or ''
        token['street'] = user.street or ''
        token['zipcode'] = user.zipcode or ''
        token['country'] = user.country or 'Philippines'

        token['profile_link'] = get_signed_cloudinary_url(user.profile_link, as_avatar=True)
        return token
    
    def validate(self, attrs):
        raw_identifier = (
            attrs.get('identifier') or 
            attrs.get('email') or 
            attrs.get('contact_number') or 
            attrs.get('username') or 
            ''
        ).strip()
        password = attrs.get('password', '')

        if not raw_identifier:
            raise serializers.ValidationError({"detail": "Please enter your email or contact number."})
        if not password:
            raise serializers.ValidationError({"detail": "Please enter your password."})

        user = None
        if '@' in raw_identifier:
            user = tbl_user_profile.objects.filter(email__iexact=raw_identifier).first()
        else:
            normalized_phone = normalize_ph_phone_number(raw_identifier)
            if normalized_phone:
                user = tbl_user_profile.objects.filter(contact_number=normalized_phone).first()
            if not user:
                user = tbl_user_profile.objects.filter(
                    Q(contact_number=raw_identifier) | Q(email__iexact=raw_identifier)
                ).first()

        if user is None or not user.check_password(password):
            from django.contrib.auth.hashers import check_password, make_password
            check_password(password, make_password('dummy_timing_defense'))
            raise AuthenticationFailed("Wrong email or password. Please try again!")

        if not user.is_active:
            raise AuthenticationFailed("This account has been deactivated or disabled.")

        if user.account_type in ['Admin', 'Barangay'] or user.is_superuser or user.is_staff:
            raise AuthenticationFailed("Wrong email or password. Please try again!")

        self.user = user

        refresh = self.get_token(user)
        return {
            "refresh": str(refresh),
            "access": str(refresh.access_token),
        }

import cloudinary.uploader

class ProfileImageUploadSerializer(serializers.ModelSerializer):
    profile_image = serializers.ImageField(
        write_only=True,
        required=True
    )

    class Meta:
        model = tbl_user_profile
        fields = ['profile_link', 'profile_image']
        read_only_fields = ['profile_link']

    def update(self, instance, validated_data):
        image_file = validated_data.pop('profile_image')
        
        upload_result = cloudinary.uploader.upload(
            image_file,
            folder="serbisure_profiles/",
            type="authenticated"
        )

        public_id = upload_result.get('public_id')
        instance.profile_link = public_id
        instance.save()
        
        return instance

    def to_representation(self, instance):
        import cloudinary.utils
        representation = super().to_representation(instance)
        representation['profile_link'] = get_signed_cloudinary_url(instance.profile_link, as_avatar=True)
        return representation

    def validate_profile_image(self, value):
        max_size = 10 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError("Image file must be under 10MB")
        return value

class UserAboutSerializer(serializers.ModelSerializer):

    class Meta:
        model = tbl_user_profile
        fields = ['user_about']

class UserTagsSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = tbl_user_profile
        fields = ['user_tags']


class ContactPrivacySerializer(serializers.ModelSerializer):

    class Meta:
        model = tbl_user_profile
        fields = ['show_contact_number']

SUPPORTED_SOCIAL_PLATFORMS = {
    'facebook': {
        'name': 'Facebook',
        'domains': ['facebook.com', 'fb.com', 'm.me', 'messenger.com', 'www.facebook.com'],
        'base_url': 'https://facebook.com/',
    },
    'instagram': {
        'name': 'Instagram',
        'domains': ['instagram.com', 'instagr.am', 'www.instagram.com'],
        'base_url': 'https://instagram.com/',
    },
    'telegram': {
        'name': 'Telegram',
        'domains': ['t.me', 'telegram.me'],
        'base_url': 'https://t.me/',
    },
    'whatsapp': {
        'name': 'WhatsApp',
        'domains': ['wa.me', 'whatsapp.com', 'api.whatsapp.com'],
        'base_url': 'https://wa.me/',
    },
    'viber': {
        'name': 'Viber',
        'domains': ['viber.com', 'chats.viber.com', 'viber.click'],
        'base_url': 'https://viber.click/',
    },
    'tiktok': {
        'name': 'TikTok',
        'domains': ['tiktok.com', 'www.tiktok.com'],
        'base_url': 'https://tiktok.com/@',
    },
    'x_twitter': {
        'name': 'X (Twitter)',
        'domains': ['twitter.com', 'x.com', 'www.twitter.com', 'www.x.com'],
        'base_url': 'https://x.com/',
    },
    'linkedin': {
        'name': 'LinkedIn',
        'domains': ['linkedin.com', 'www.linkedin.com'],
        'base_url': 'https://linkedin.com/in/',
    },
    'youtube': {
        'name': 'YouTube',
        'domains': ['youtube.com', 'youtu.be', 'www.youtube.com'],
        'base_url': 'https://youtube.com/@',
    },
}


class UserSocialLinksSerializer(serializers.ModelSerializer):
    social_links = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        allow_empty=True
    )
    show_social_links = serializers.BooleanField(required=False)

    class Meta:
        model = tbl_user_profile
        fields = ['social_links', 'show_social_links']

    def validate_social_links(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Social links must be a list of link objects.")

        if len(value) > 5:
            raise serializers.ValidationError("You can add a maximum of 5 social accounts.")

        cleaned = []
        seen_platforms = set()
        seen_urls = set()

        for idx, item in enumerate(value):
            if not isinstance(item, dict):
                raise serializers.ValidationError(f"Item {idx + 1} must be an object with 'platform' and 'url'.")

            raw_platform = str(item.get('platform', '') or '').strip().lower()
            raw_url = str(item.get('url', '') or '').strip()

            if not raw_url:
                continue

            if '\x00' in raw_url:
                raise serializers.ValidationError(f"URL at item {idx + 1} cannot contain null characters.")

            # Block malicious URI schemes (XSS protection)
            if re.match(r'^(javascript|data|file|vbscript):', raw_url, re.IGNORECASE):
                raise serializers.ValidationError(f"Unsafe URL scheme detected at item {idx + 1}.")

            # Auto-normalize handle input (e.g. '@maria_santos')
            if raw_url.startswith('@'):
                handle = raw_url.lstrip('@')
                if raw_platform in SUPPORTED_SOCIAL_PLATFORMS:
                    raw_url = SUPPORTED_SOCIAL_PLATFORMS[raw_platform]['base_url'] + handle
                else:
                    raw_url = 'https://' + raw_url.lstrip('@')
            elif not raw_url.startswith(('http://', 'https://')):
                raw_url = 'https://' + raw_url

            parsed = urlparse(raw_url)
            netloc = (parsed.netloc or '').lower().split(':')[0]
            if not netloc or '.' not in netloc:
                raise serializers.ValidationError(f"Invalid domain in URL at item {idx + 1}: '{raw_url}'")

            # Block private IP / loopback probing (SSRF protection)
            if netloc in ['localhost', '127.0.0.1', '0.0.0.0'] or netloc.startswith(('192.168.', '10.', '172.16.', '172.17.', '172.18.', '172.19.', '172.2', '172.3', '169.254.')):
                raise serializers.ValidationError(f"Private network address not allowed at item {idx + 1}.")

            # Detect platform if not explicitly provided or standardize platform key
            detected_platform = raw_platform
            for p_key, meta in SUPPORTED_SOCIAL_PLATFORMS.items():
                if any(d in netloc for d in meta['domains']):
                    detected_platform = p_key
                    break

            if not detected_platform:
                detected_platform = 'website'

            platform_info = SUPPORTED_SOCIAL_PLATFORMS.get(detected_platform, {
                'name': detected_platform.replace('_', ' ').capitalize(),
                'domains': [],
                'base_url': ''
            })

            clean_url = parsed.geturl()
            if len(clean_url) > 255:
                raise serializers.ValidationError(f"URL at item {idx + 1} exceeds maximum length of 255 characters.")

            # Deduplication
            if detected_platform in seen_platforms and detected_platform != 'website':
                raise serializers.ValidationError(f"You have already added a {platform_info['name']} account.")
            if clean_url in seen_urls:
                raise serializers.ValidationError(f"Duplicate link detected: '{clean_url}'")

            seen_platforms.add(detected_platform)
            seen_urls.add(clean_url)

            # Derive clean username or handle from path for display
            path_cleaned = parsed.path.strip('/')
            display_handle = path_cleaned.split('/')[-1] if path_cleaned else netloc

            cleaned.append({
                'platform': detected_platform,
                'platform_name': platform_info['name'],
                'url': clean_url,
                'handle': display_handle
            })

        return cleaned


class JobStatusSerializer(serializers.ModelSerializer):

    class Meta:
        model = tbl_user_profile
        fields = ['is_on_job']


class KasambahayResumeSerializer(serializers.ModelSerializer):
    resume_pdf = serializers.FileField(
        write_only=True,
        required=False
    )
    resume_url = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = tbl_user_profile
        fields = ['resume_pdf', 'resume_url', 'resume_uploaded_at']
        read_only_fields = ['resume_url', 'resume_uploaded_at']

    def validate_resume_pdf(self, value):
        if not value:
            raise serializers.ValidationError("A PDF file is required.")

        name = getattr(value, 'name', '')
        if not name.lower().endswith('.pdf'):
            raise serializers.ValidationError("Only PDF files are accepted.")

        max_size = 10 * 1024 * 1024
        if value.size > max_size:
            raise serializers.ValidationError("Resume file must be under 10MB.")

        return value

    def validate(self, attrs):
        request = self.context.get('request')
        if request and request.method in ['PATCH', 'PUT', 'POST']:
            if 'resume_pdf' not in attrs:
                raise serializers.ValidationError({"resume_pdf": "Please provide a PDF file."})
        return attrs

    def get_resume_url(self, obj):
        if not obj.resume_url:
            return None
        import cloudinary.utils
        try:
            # Backward compatibility: previously uploaded raw files end with .pdf in public_id
            if obj.resume_url.endswith('.pdf'):
                temporary_url, _ = cloudinary.utils.cloudinary_url(
                    obj.resume_url,
                    resource_type="raw",
                    type="authenticated",
                    sign_url=True,
                )
            else:
                temporary_url, _ = cloudinary.utils.cloudinary_url(
                    obj.resume_url,
                    resource_type="image",
                    format="pdf",
                    type="authenticated",
                    sign_url=True,
                )
            return temporary_url
        except Exception:
            return None

    def update(self, instance, validated_data):
        from django.utils import timezone
        import cloudinary.uploader

        pdf_file = validated_data.pop('resume_pdf', None)
        if pdf_file:
            upload_result = cloudinary.uploader.upload(
                pdf_file,
                folder="serbisure_resumes/",
                resource_type="image",
                format="pdf",
                type="authenticated",
                use_filename=True,
                unique_filename=True
            )
            public_id = upload_result.get('public_id')
            instance.resume_url = public_id
            instance.resume_uploaded_at = timezone.now()
            instance.save(update_fields=['resume_url', 'resume_uploaded_at'])

        return instance


class PublicProfileSerializer(serializers.ModelSerializer):
    full_name = serializers.SerializerMethodField()
    profile_link = serializers.SerializerMethodField()
    resume_url = serializers.SerializerMethodField()

    class Meta:
        model = tbl_user_profile
        fields = [
            'id',
            'first_name',
            'last_name',
            'full_name',
            'account_type',
            'verification_status',
            'profile_link',
            'resume_url',
            'user_about',
            'user_tags',
            'region',
            'province',
            'city',
            'barangay',
            'street',
            'zipcode',
            'country',
            'date_joined',
            'social_links',
            'show_social_links',
            'cancellation_strikes',
            'is_restricted',
        ]
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if getattr(instance, 'show_contact_number', False):
            data['contact_number'] = instance.contact_number
            data['show_contact_number'] = True
        else:
            data['show_contact_number'] = False

        if getattr(instance, 'show_social_links', True):
            data['social_links'] = getattr(instance, 'social_links', []) or []
            data['show_social_links'] = True
        else:
            data['social_links'] = []
            data['show_social_links'] = False

        return data

    def get_full_name(self, obj):
        parts = [obj.first_name, obj.middle_name, obj.last_name]
        return ' '.join(p for p in parts if p).strip()

    def get_profile_link(self, obj):
        return get_signed_cloudinary_url(obj.profile_link, as_avatar=True)

    def get_resume_url(self, obj):
        if not obj.resume_url or obj.account_type != 'Kasambahay':
            return None
        import cloudinary.utils
        try:
            if obj.resume_url.endswith('.pdf'):
                temporary_url, _ = cloudinary.utils.cloudinary_url(
                    obj.resume_url,
                    resource_type="raw",
                    type="authenticated",
                    sign_url=True,
                )
            else:
                temporary_url, _ = cloudinary.utils.cloudinary_url(
                    obj.resume_url,
                    resource_type="image",
                    format="pdf",
                    type="authenticated",
                    sign_url=True,
                )
            return temporary_url
        except Exception:
            return None
