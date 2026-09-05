from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.db.models import CheckConstraint, Q
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from django.db.models.expressions import RawSQL
import uuid 

def validate_user_tags(value):

    if not isinstance(value, list):
        raise ValidationError("User tags must be a list")
    
    if len(value) > 10:
        raise ValidationError("You can add a maximum of 10 tags.")

    for tag in value:
        if not isinstance(tag, str):
            raise ValidationError("Each user tags must be text")
        
        if len(tag) > 15:
            raise ValidationError(f"Tag '{tag}' exceeds 15 characters")


class CustomUserManager(BaseUserManager):

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The email field must be set')
        
        email = self.normalize_email(email)
        desired_status = extra_fields.pop('verification_status', None)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)

        if desired_status == 'Verified':
            from verifications.models import tbl_documents
            if user.account_type == 'Homeowner':
                tbl_documents.objects.get_or_create(
                    user_profile=user,
                    document_type='national_id_front',
                    defaults={'document_url': 'seed_id_front', 'verification_status': 'Verified'}
                )
                tbl_documents.objects.get_or_create(
                    user_profile=user,
                    document_type='national_id_back',
                    defaults={'document_url': 'seed_id_back', 'verification_status': 'Verified'}
                )
            elif user.account_type == 'Kasambahay':
                tbl_documents.objects.get_or_create(
                    user_profile=user,
                    document_type='nbi_clearance',
                    defaults={'document_url': 'seed_nbi', 'verification_status': 'Verified'}
                )
                tbl_documents.objects.get_or_create(
                    user_profile=user,
                    document_type='police_clearance',
                    defaults={'document_url': 'seed_police', 'verification_status': 'Verified'}
                )
        return user

    
    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('account_type', 'Admin')

        return self.create_user(email, password, **extra_fields)


class tbl_user_profile(AbstractUser):

    # Define our ENUM choices up here 

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'first_name', 'middle_name', 'last_name']

    ACCOUNT_TYPE_CHOICES = (
        ('Kasambahay', 'Kasambahay'),
        ('Homeowner', 'Homeowner'),
        ('Barangay', 'Barangay'),
        ('Admin', 'Admin')
    )

    VERIFICATION_STATUS_CHOICES = (
        ('Unverified', 'Unverified'),
        ('Pending', 'Pending'),
        ('Verified', 'Verified'),
        ('Rejected', 'Rejected')
    )

    GENDER_CHOICES = (
        ('Male', 'Male'),
        ('Female', 'Female'),
        ('Other', 'Other')
    )

    # Note: first_name, last_name, email, password, and 
    # date_joined are already built-in because we are using AbstractUser

    # Custom text and date fields

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    email = models.EmailField(
        unique=True
    )



    first_name = models.CharField(
        max_length=100, 
        blank=False, 
        null=False
    )


    middle_name = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )

    last_name = models.CharField(
        max_length=100,
        blank=False,
        null=False
    )

    is_active = models.BooleanField(
        default=True,
    )

    is_staff = models.BooleanField(
        default=False,
        help_text="Designates weather the user c an log into admin"
    )

    date_of_birth = models.DateField(
        blank=True, 
        null=True
    )

    religion = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )

    nationality = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )
    
    street = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )

    city = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )

    province = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )
    
    zipcode = models.CharField(
        max_length=4, 
        blank=True, 
        null=True,
        validators=[
            RegexValidator(
                regex=r'^\d{4}$',
                message='Zipcode must be exactly 4 digits (e.g 1000)'
            )
        ]
    )

    country = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )

    gender = models.CharField(
        max_length=20, 
        choices=GENDER_CHOICES,
        blank=True, 
        null=True
    )

    language = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )

    profile_link = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    resume_url = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="Cloudinary public_id of the uploaded PDF resume"
    )

    resume_uploaded_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text="Timestamp of the last resume upload"
    )

    # Custome ENUM fields using the choices we defined above

    account_type = models.CharField(
        max_length=20,
        choices=ACCOUNT_TYPE_CHOICES,
        default='Homeowner'
    )

    @property
    def verification_status(self) -> str:
        """
        Dynamically derived verification status based on submitted documents in tbl_documents.
        Eliminates denormalization and database desynchronization.
        - Homeowner: Verified when BOTH 'national_id_front' and 'national_id_back' are Verified.
        - Kasambahay: Verified when BOTH 'nbi_clearance' and 'police_clearance' are Verified.
        - Admin / Barangay: Automatically 'Verified'.
        - Otherwise: 'Pending' if any document is Pending, 'Rejected' if any is Rejected, else 'Unverified'.
        """
        if hasattr(self, '_override_verification_status') and self._override_verification_status is not None:
            return self._override_verification_status

        if self.account_type in ['Admin', 'Barangay']:
            return 'Verified'

        try:
            user_docs = list(self.documents.all())
        except Exception:
            return 'Unverified'

        if not user_docs:
            return 'Unverified'

        verified_types = {
            doc.document_type
            for doc in user_docs
            if doc.verification_status == 'Verified'
        }

        if self.account_type == 'Homeowner':
            required = {'national_id_front', 'national_id_back'}
            if required.issubset(verified_types):
                return 'Verified'
        elif self.account_type == 'Kasambahay':
            required = {'nbi_clearance', 'police_clearance'}
            if required.issubset(verified_types):
                return 'Verified'

        doc_statuses = {doc.verification_status for doc in user_docs}
        if 'Rejected' in doc_statuses:
            return 'Rejected'
        if 'Pending' in doc_statuses:
            return 'Pending'
        if len(verified_types) > 0:
            return 'Pending'

        return 'Unverified'

    @verification_status.setter
    def verification_status(self, value):
        self._override_verification_status = value

    contact_number = models.CharField(
            max_length=13,
            blank=False,
            null=False,
            validators=[
                RegexValidator(
                    regex=r'^\+639\d{9}$', 
                    message="Phone number must start with '+63' followed by 10 digits (e.g., +639123456789)."
                )
            ],
            help_text="User's contact phone number (e.g. +639123456789)"
        )

    user_about = models.TextField(
        max_length=500,
        blank=True,
        null=False,
        default='No Bio'
    )

    user_tags = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_user_tags]
    )

    def __str__(self):
        return self.username
    
    class Meta: 
        constraints = [
            # Lock down the account_type column in the database

            CheckConstraint(
                condition=Q(account_type__in=['Kasambahay', 'Homeowner', 'Barangay', 'Admin']),
                name='valid_account_type_enum'
            ),

            CheckConstraint(
                condition=Q(gender__in=['Male', 'Female', 'Other']),
                name='valid_gender_enum'
            ),

            CheckConstraint(
                condition=RawSQL("jsonb_typeof(user_tags) = 'array'", [], output_field=models.BooleanField()),
                name='valid_user_tags_must_be_array'
            )
        ]