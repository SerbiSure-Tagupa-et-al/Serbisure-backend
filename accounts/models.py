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
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
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

    verification_status = models.CharField(
        max_length=20,
        choices=VERIFICATION_STATUS_CHOICES,
        default='Unverified'
    )

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
                condition=Q(verification_status__in=['Unverified', 'Pending', 'Verified', 'Rejected']),
                name='valid_verification_status_enum'
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