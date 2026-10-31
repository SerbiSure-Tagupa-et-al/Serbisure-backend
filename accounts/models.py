from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models, transaction
from django.db.models import CheckConstraint, Q
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from django.db.models.expressions import RawSQL
from .penalties import current_penalty_month
import uuid 
from .penalty_models import PenaltyAdjustment as PenaltyAdjustment  # Register and re-export the feature model.


class tbl_worker_status_history(models.Model):
    """Immutable status and eligibility at an event; retained after account deletion."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    worker = models.ForeignKey('tbl_user_profile', on_delete=models.SET_NULL, null=True, related_name='status_history')
    worker_id_snapshot = models.UUIDField(editable=False)
    status = models.BooleanField(help_text='True: Occupied; False: Available')
    barangay = models.CharField(max_length=100, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)
    is_verified = models.BooleanField(default=False, help_text='Active worker with both required clearances approved at this event')
    clearance_valid_until = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'tbl_worker_status_history'
        indexes = [models.Index(fields=['worker_id_snapshot', '-changed_at'], name='worker_status_latest_idx')]


class tbl_worker_profile(models.Model):
    """Self-reported worker preferences; separate from agreed booking terms."""
    user = models.OneToOneField('tbl_user_profile', on_delete=models.CASCADE, related_name='worker_profile')
    service_categories = models.JSONField(default=list, blank=True)
    work_setup = models.CharField(max_length=10, blank=True, choices=[('stay_in', 'Stay-in'), ('stay_out', 'Stay-out'), ('either', 'Either')])
    engagement_preference = models.CharField(max_length=10, blank=True, choices=[('short_term', 'Short-term'), ('long_term', 'Long-term'), ('both', 'Both')])
    desired_monthly_rate = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    desired_daily_rate = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    civil_status = models.CharField(max_length=15, blank=True, choices=[(value, value) for value in ('Single', 'Married', 'Widowed', 'Separated')])
    number_of_children = models.PositiveSmallIntegerField(null=True, blank=True)
    experience_level = models.CharField(max_length=10, blank=True, choices=[('new', 'First-time worker'), ('household', 'Own household experience'), ('paid', 'Previous paid work')])
    experience_duration = models.CharField(max_length=10, blank=True, choices=[('under_1', 'Less than 1 year'), ('1_to_3', '1-3 years'), ('over_3', 'More than 3 years')])
    experience_summary = models.CharField(max_length=300, blank=True)
    practical_skills = models.JSONField(default=list, blank=True)
    available_from = models.DateField(null=True, blank=True)
    available_days = models.JSONField(default=list, blank=True)
    preferred_shifts = models.JSONField(default=list, blank=True)
    preferred_work_areas = models.JSONField(default=list, blank=True)
    education = models.CharField(max_length=100, blank=True)
    training = models.JSONField(default=list, blank=True)
    references_available = models.BooleanField(default=False)
    share_family_details = models.BooleanField(default=False)
    share_contact_details = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tbl_worker_profile'
        constraints = [
            models.CheckConstraint(condition=Q(number_of_children__isnull=True) | Q(number_of_children__lte=99), name='worker_children_0_to_99'),
            models.CheckConstraint(condition=Q(desired_monthly_rate__isnull=True) | Q(desired_monthly_rate__gt=0), name='worker_monthly_rate_positive'),
            models.CheckConstraint(condition=Q(desired_daily_rate__isnull=True) | Q(desired_daily_rate__gt=0), name='worker_daily_rate_positive'),
        ]

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


def validate_social_links(value):
    if not isinstance(value, list):
        raise ValidationError("Social links must be a list.")
    
    if len(value) > 5:
        raise ValidationError("You can add a maximum of 5 social accounts.")

    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("Each social link must be an object containing 'platform' and 'url'.")
        url = item.get('url', '')
        if not isinstance(url, str) or len(url) > 255:
            raise ValidationError("Each social link URL must be a string up to 255 characters.")


class CustomUserManager(BaseUserManager):

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The email field must be set')
        
        email = self.normalize_email(email)
        desired_status = extra_fields.pop('verification_status', None)

        if not extra_fields.get('contact_number'):
            import hashlib
            h = int(hashlib.md5(email.encode('utf-8')).hexdigest()[:8], 16) % 900000000 + 100000000
            extra_fields['contact_number'] = f"+639{h}"

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
    
    region = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Philippine region (e.g. Region X - Northern Mindanao)"
    )

    province = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )

    city = models.CharField(
        max_length=100, 
        blank=True, 
        null=True
    )

    barangay = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Barangay name within the city/municipality"
    )

    street = models.CharField(
        max_length=100, 
        blank=True, 
        null=True,
        help_text="House/Lot/Block number, street name, zone, or subdivision only. NOT the barangay."
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
        - Only the latest required submissions count; expired credentials are not Verified.
        - Expired eligibility maps to Unverified for mobile API compatibility.
        """
        if hasattr(self, '_override_verification_status') and self._override_verification_status is not None:
            return self._override_verification_status

        if self.account_type in ['Admin', 'Barangay']:
            return 'Verified'

        from .workforce import credential_snapshot
        # Keep the mobile API's existing enum. Expired credentials remove job
        # eligibility immediately, without changing a document's review decision.
        status = credential_snapshot(self)[0]
        return {'verified': 'Verified', 'pending': 'Pending', 'rejected': 'Rejected',
                'expired': 'Unverified', 'unverified': 'Unverified'}[status]

    @verification_status.setter
    def verification_status(self, value):
        self._override_verification_status = value

    contact_number = models.CharField(
            max_length=13,
            unique=True,
            blank=False,
            null=False,
            validators=[
                RegexValidator(
                    regex=r'^\+639\d{9}$', 
                    message="Phone number must start with '+63' followed by 10 digits (e.g., +639123456789)."
                )
            ],
            help_text="User's unique contact phone number (e.g. +639123456789)"
        )

    show_contact_number = models.BooleanField(
        default=False,
        help_text="Controls if contact number is visible to other users on public profile"
    )

    is_on_job = models.BooleanField(
        default=False,
        help_text="Kasambahay employment status: True if On the Job, False if Available"
    )

    chat_suspended_until = models.DateTimeField(null=True, blank=True)
    is_account_suspended = models.BooleanField(default=False)
    suspended_by = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name='suspensions_issued')

    penalty_points = models.IntegerField(
        default=0,
        help_text="Accumulated booking cancellation penalty points"
    )

    penalty_month = models.DateField(default=current_penalty_month)

    is_restricted = models.BooleanField(
        default=False,
        help_text="Indicates whether user account is restricted from bookings"
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

    social_links = models.JSONField(
        default=list,
        blank=True,
        validators=[validate_social_links],
        help_text="User's external social contact links (Facebook, Instagram, Telegram, etc.)"
    )

    show_social_links = models.BooleanField(
        default=True,
        help_text="Controls if social links are visible to other users on public profile"
    )

    def clean(self):
        super().clean()
        if self.contact_number:
            from core.utils import normalize_ph_phone_number
            normalized = normalize_ph_phone_number(self.contact_number)
            if normalized:
                self.contact_number = normalized

        # Enforce exactly one active Barangay LGU account per barangay
        if self.account_type == 'Barangay' and self.barangay:
            import re
            clean_b = re.sub(r'^(brgy\.?|barangay)\s+', '', self.barangay, flags=re.IGNORECASE).strip().lower()
            existing_conflict = tbl_user_profile.objects.filter(
                account_type='Barangay'
            ).exclude(pk=self.pk)
            for ex in existing_conflict:
                ex_clean = re.sub(r'^(brgy\.?|barangay)\s+', '', ex.barangay or '', flags=re.IGNORECASE).strip().lower()
                if ex_clean == clean_b:
                    raise ValidationError({
                        'barangay': f"An official LGU account for Barangay '{self.barangay}' already exists. Only one LGU account per barangay is allowed."
                    })

    def save(self, *args, **kwargs):
        if self.contact_number:
            from core.utils import normalize_ph_phone_number
            normalized = normalize_ph_phone_number(self.contact_number)
            if normalized:
                self.contact_number = normalized

        # Standardize barangay name for Barangay LGU accounts and prevent duplicate save
        if self.account_type == 'Barangay' and self.barangay:
            import re
            clean_b = re.sub(r'^(brgy\.?|barangay)\s+', '', self.barangay, flags=re.IGNORECASE).strip()
            self.barangay = clean_b.title()

            existing_conflict = tbl_user_profile.objects.filter(
                account_type='Barangay',
                barangay__iexact=self.barangay
            ).exclude(pk=self.pk).exists()
            if existing_conflict:
                from django.db import IntegrityError
                raise IntegrityError(f"unique_lgu_account_per_barangay: An account for Barangay '{self.barangay}' already exists.")
        if self.account_type in ['Barangay', 'Admin']:
            self.is_staff = True

        with transaction.atomic(using=kwargs.get('using') or 'default'):
            super().save(*args, **kwargs)
            from .workforce import record_worker_status
            record_worker_status(self.pk, using=self._state.db)

    def __str__(self):
        return self.username
    
    class Meta: 
        verbose_name = "User"
        verbose_name_plural = "Users"
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
            ),

            CheckConstraint(
                condition=RawSQL("jsonb_typeof(social_links) = 'array'", [], output_field=models.BooleanField()),
                name='valid_social_links_must_be_array'
            ),

            models.UniqueConstraint(
                fields=['contact_number'],
                name='unique_tbl_user_profile_contact_number'
            ),

            # Enforce strictly ONE account per barangay for Barangay LGU accounts
            models.UniqueConstraint(
                fields=['barangay'],
                condition=Q(account_type='Barangay') & ~Q(barangay__isnull=True) & ~Q(barangay=''),
                name='unique_lgu_account_per_barangay'
            ),

            CheckConstraint(
                condition=Q(contact_number__regex=r'^\+639\d{9}$'),
                name='valid_contact_number_e164_format'
            )
        ]
