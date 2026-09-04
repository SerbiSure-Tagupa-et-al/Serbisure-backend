from rest_framework import serializers
from .models import tbl_user_profile
from datetime import date
from core.utils import convert_title, check_input_letters
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework.exceptions import AuthenticationFailed, ValidationError
import uuid

class UserRegistrationSerializer(serializers.ModelSerializer):
    # This enrsure the password is required to create an account,
    # but the API will never will accidentally send it back to the frontend

    password = serializers.CharField(write_only=True)

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
            'street',
            'city',
            'province',
            'zipcode',
            'country',
            'gender',
            'contact_number',
            'language',
            'email',
            'password',
            'account_type',
            'verification_status', 
        ]

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

        user = tbl_user_profile(**validated_data)
        user.set_password(password)
        user.save()

        return user
    
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

        # 1. Check length 
        if len(value) < 11: 
            raise serializers.ValidationError("Password must be at least 11 characters long.")

        if len(value) > 30:
            raise serializers.ValidationError("Password cannot exceed 30 characters.")

        # 2. Check for at least one number 
        if not any(char.isdigit() for char in value):
            raise serializers.ValidationError("Password must contain at least one number")

        # 3. Check for at least one letter 
        if not any(char.isalpha() for char in value):
            raise serializers.ValidationError("Password must contain at least one letter")
        
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
        return value.lower()

    def validate_contact_number(self, value):
        if not value.startswith('+639'):
            raise serializers.ValidationError("Contact number must strictly start with +63.")
        
        if len(value) != 13: 
            raise serializers.ValidationError("Contact number must be exactly 13 characters long (e.g., +639123456789).")
            
        if not value[1:].isdigit():
            raise serializers.ValidationError("Contact number must only contain numbers after the + sign.")
            
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

        public_id = user.profile_link

        if not public_id:
            return token
        
        temporary_url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            type="authenticated",
            sign_url=True
        )

        token['profile_link'] = temporary_url

        return token
    

class CustomLoginSerializer(TokenObtainPairSerializer):
    default_error_messages = {
        "no_active_account": "Wrong email or password. Please try again!"
    }

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
    
        token['first_name'] = user.first_name
        token['middle_name'] = user.middle_name
        token['last_name'] = user.last_name
        token['account_type'] = user.account_type
        token['verification_status'] = user.verification_status
        token['language'] = user.language

        public_id = user.profile_link

        if not public_id:
            return token
        
        temporary_url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            type="authenticated",
            sign_url=True
        )

        token['profile_link'] = temporary_url

        return token
    
    def validate(self, attrs):
        # This will verify the email and password first
        data = super().validate(attrs)

        # self.user is populated if the email/password were correct
        # We block 'Admin' and 'Barangay' accounts from logging into the mobile app
        if self.user.account_type in ['Admin', 'Barangay'] or self.user.is_superuser or self.user.is_staff:
            # We return the exact same generic error so attackers don't know it's an admin account
            raise AuthenticationFailed("Wrong email or password. Please try again!")

        return data

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
        public_id = instance.profile_link

        if public_id:
            temporary_url, _ = cloudinary.utils.cloudinary_url(
                public_id,
                type="authenticated",
                sign_url=True,
            )
            representation['profile_link'] = temporary_url

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
            'city',
            'province',
            'date_joined',
        ]
        read_only_fields = fields

    def get_full_name(self, obj):
        parts = [obj.first_name, obj.middle_name, obj.last_name]
        return ' '.join(p for p in parts if p).strip()

    def get_profile_link(self, obj):
        if not obj.profile_link:
            return None
        import cloudinary.utils
        try:
            temporary_url, _ = cloudinary.utils.cloudinary_url(
                obj.profile_link,
                type="authenticated",
                sign_url=True,
            )
            return temporary_url
        except Exception:
            return None

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