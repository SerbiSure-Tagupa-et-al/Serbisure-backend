from django.db import models
from django.conf import settings
from django.db.models import CheckConstraint, Q, F
from django.core.validators import RegexValidator, MinValueValidator
from decimal import Decimal
from django.contrib.postgres.fields import ArrayField
import uuid 
import re

# Create your models here.

class tbl_booking(models.Model):

    SERVICES_OFFER_CHOICES = (
        ('Cleaning', 'Cleaning'),
        ('Child_care', 'Child Care'),
        ('Cooking', 'Cooking'),
        ('Caregiver','Caregiver'),
        ('Laundry', 'Laundry'),
        ('All-around', 'All around')
    )

    BOOKING_TYPE_CHOICES = (
        ('short_term', 'Short Term'),
        ('long_term', 'Long Term')
    )

    WORK_SETUP_CHOICES = (('stay_in', 'Stay-in'), ('stay_out', 'Stay-out'))

    BOOKING_STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Accepted', 'Accepted'),
        ('InProgress', 'In Progress'),
        ('Completed', 'Completed'),
        ('Cancelled', 'Cancelled')
    )

    booking_id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False
    )

    poster_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='user_post',
        limit_choices_to={'account_type__in': ['Homeowner', 'Kasambahay']},
        db_column='poster_id'
    )

    booking_type = models.CharField(
        max_length=20,
        choices=BOOKING_TYPE_CHOICES,
        blank=False,
        null=False
    )

    work_setup = models.CharField(
        max_length=8, choices=WORK_SETUP_CHOICES, null=True, blank=True,
        help_text='Agreed work setup; null only for legacy posts without an unambiguous setup.'
    )

    recipient_id = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='received_booking_offers', null=True, blank=True)
    offer_request_key = models.UUIDField(null=True, blank=True, editable=False)
    offer_payload_hash = models.CharField(max_length=64, blank=True, default='', editable=False)
    work_days = ArrayField(models.CharField(max_length=2, choices=[(day, day) for day in ['M', 'T', 'W', 'Th', 'F', 'S', 'Su']]), default=list, blank=True)
    daily_start_time = models.TimeField(null=True, blank=True)
    daily_end_time = models.TimeField(null=True, blank=True)

    booking_status = models.CharField(
        max_length=20,
        choices=BOOKING_STATUS_CHOICES,
        default='Pending',
        blank=False,
        null=False
    )

    service_category = ArrayField(
        models.CharField(
            max_length=50,
            choices=SERVICES_OFFER_CHOICES
        ),
        blank=False,
        null=False
    )

    start_time = models.DateTimeField(
        blank=False,
        null=False
    )

    end_time = models.DateTimeField(
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
        help_text="Barangay name within city/municipality"
    )

    street = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="House No. / Street / Zone / Subdivision"
    )

    floor_number = models.CharField(
        max_length=50,
        blank=True,
        null=True
    )

    zip_code = models.CharField(
        max_length=4,
        blank=True,
        null=True,
        default='',
        validators=[
            RegexValidator(
                r'^(0[4-9]\d{2}|[1-9]\d{3})$', 
                'Must be a valid 4-digit Philippine Zip Code (0400 or higher).'
            ),
        ]
    )

    special_instruction = models.TextField(
        max_length=1000,
        blank=True,
        null=True
    )

    rate_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal('1.00'))
        ],
        null=False,
        blank=False
    )

    rate_period = models.CharField(max_length=5, choices=[('day', 'Day'), ('month', 'Month')])
    rate_needs_review = models.BooleanField(default=False)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True, null=True, default='')
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='cancelled_bookings', db_column='cancelled_by')
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_penalty_points = models.PositiveSmallIntegerField(default=0)

    createdAt = models.DateTimeField(
        auto_now_add=True
    )

    @property
    def full_address(self):
        barangay = re.sub(r'^(?:brgy\.?|barangay)\s+', '', (self.barangay or '').strip(), flags=re.IGNORECASE)
        parts = [self.street, self.floor_number, f'Brgy. {barangay}' if barangay else '',
                 self.city, self.province, self.zip_code]
        return ', '.join(str(part).strip() for part in parts if part and str(part).strip())

    def __str__(self):
        return f"{self.service_category} - {self.booking_status}"

    
    class Meta:
        db_table = 'tbl_booking'

        constraints = [
            models.UniqueConstraint(fields=['poster_id', 'offer_request_key'], name='unique_chat_offer_request'),
            CheckConstraint(
                condition=Q(work_setup__isnull=True) | Q(work_setup__in=['stay_in', 'stay_out']),
                name='valid_booking_work_setup',
            ),

            CheckConstraint(
                condition=Q(booking_type__in=['short_term', 'long_term']),
                name='valid_booking_type_enum'
            ),

            CheckConstraint(
                condition=Q(booking_status__in=['Pending', 'Accepted', 'InProgress', 'Completed', 'Cancelled']),
                name='valid_booking_status_enum'
            ),

            CheckConstraint(
                condition=Q(service_category__contained_by=[
                    'Cleaning', 'Child_care', 'Cooking', 'Caregiver', 'Laundry', 'All-around']),
                    name='validate_service_category_array'
            ),

            CheckConstraint(
                condition=Q(service_category__len__gt=0),
                name='service_category_not_empty'
            ),

            CheckConstraint(
                condition=Q(end_time__gt=F('start_time')) | Q(end_time__isnull=True),
                name='end_time_after_start_time'
            ),
            
            CheckConstraint(
                condition=Q(rate_amount__gte=Decimal('1.00')),
                name='valid_rate_amount'
            ),
            CheckConstraint(
                condition=(Q(booking_type='short_term', rate_period='day') |
                           Q(booking_type='long_term', rate_period='month')),
                name='booking_rate_period_matches_type'
            ),
        ]

class tbl_booking_assignment(models.Model):
    booking_assignment_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    booking_id = models.ForeignKey(
        'tbl_booking', # Links to the model right above it
        on_delete=models.CASCADE,
        related_name='assignments',
        db_column='booking_id'
    )
    
    accepter_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='accepted_bookings',
        limit_choices_to={'account_type__in': ['Homeowner', 'Kasambahay']},
        db_column='accepter_id'
    )
    
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tbl_booking_assignment'
        constraints = [models.UniqueConstraint(fields=['booking_id'], name='one_assignment_per_booking')]


class tbl_booking_proposal(models.Model):
    PROPOSAL_STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Accepted', 'Accepted'),
        ('Rejected', 'Rejected'),
        ('Withdrawn', 'Withdrawn')
    )

    proposal_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking_id = models.ForeignKey(
        'tbl_booking',
        on_delete=models.CASCADE,
        related_name='proposals',
        db_column='booking_id'
    )
    proposer_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='booking_proposals',
        limit_choices_to={'account_type__in': ['Homeowner', 'Kasambahay']},
        db_column='proposer_id'
    )
    proposed_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal('1.00'))
        ]
    )
    message = models.TextField(max_length=500, blank=True, null=True)
    status = models.CharField(max_length=20, choices=PROPOSAL_STATUS_CHOICES, default='Pending')
    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tbl_booking_proposal'
        constraints = [
            CheckConstraint(
                condition=Q(status__in=['Pending', 'Accepted', 'Rejected', 'Withdrawn']),
                name='valid_proposal_status_enum'
            ),
            CheckConstraint(
                condition=Q(proposed_rate__gte=Decimal('1.00')),
                name='valid_proposal_rate'
            )
        ]


class tbl_saved_post(models.Model):
    saved_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='saved_posts',
        db_column='user_id'
    )
    booking = models.ForeignKey(
        'tbl_booking',
        on_delete=models.CASCADE,
        related_name='saved_by_users',
        db_column='booking_id'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tbl_saved_post'
        constraints = [
            models.UniqueConstraint(fields=['user', 'booking'], name='unique_user_saved_booking')
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"User {self.user_id} saved {self.booking_id}"
