from django.db import models
from django.conf import settings
from django.db.models import CheckConstraint, Q, F
from django.core.validators import RegexValidator, MinValueValidator
from decimal import Decimal
from django.contrib.postgres.fields import ArrayField
import uuid 

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
        default='9000',
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

    daily_rate = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal('1.00'))
        ],
        null=False,
        blank=False
    )

    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cancelled_bookings',
        db_column='cancelled_by'
    )

    cancel_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cancel_requested_bookings',
        db_column='cancel_requested_by'
    )

    cancel_requested_at = models.DateTimeField(
        null=True,
        blank=True
    )

    cancellation_reason = models.CharField(
        max_length=255,
        blank=True,
        null=True
    )

    createdAt = models.DateTimeField(
        auto_now_add=True
    )

    @property
    def full_address(self):
        parts = [p for p in [self.street, self.barangay, self.city, self.province] if p]
        return ", ".join(parts) if parts else ""

    def __str__(self):
        return f"{self.service_category} - {self.booking_status}"

    
    class Meta:
        db_table = 'tbl_booking'

        constraints = [

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
                condition=Q(daily_rate__gte=Decimal('1.00')),
                name='valid_daily_rate'
            )
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