from rest_framework import serializers
from decimal import Decimal
from .models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal
from .wage_policy import get_minimum_daily_wage, get_monthly_equivalent
from django.utils import timezone
from reviews.models import tbl_review
from django.db.models import Avg
from core.utils import get_signed_cloudinary_url


def get_signed_avatar(user):
    if not user:
        return None
    public_id = getattr(user, 'profile_link', None)
    return get_signed_cloudinary_url(public_id, as_avatar=True)


class BookingSerializer(serializers.ModelSerializer):
    zip_code = serializers.CharField(max_length=4, required=False, default='9000', allow_blank=True)
    region = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    province = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    barangay = serializers.CharField(max_length=100, required=False, allow_blank=True, allow_null=True)
    street = serializers.CharField(max_length=255, required=False, allow_blank=True, allow_null=True)
    full_address = serializers.ReadOnlyField()

    class Meta:
        model = tbl_booking
        fields = [
            'booking_id',
            'booking_type',
            'booking_status',
            'service_category',
            'start_time',
            'end_time',
            'region',
            'province',
            'city',
            'barangay',
            'street',
            'full_address',
            'floor_number',
            'zip_code',
            'special_instruction',
            'daily_rate',
            'poster_id',
            'createdAt'
        ]
        read_only_fields = ['booking_id', 'booking_status', 'poster_id', 'createdAt']

    def validate(self, data):
        # Gracefully handle and discard deprecated service_address if passed
        data.pop('service_address', None)

        # Auto-normalize service_category: if all 5 base services are selected or All-around is combined, normalize to ['All-around']
        service_category = data.get('service_category')
        if isinstance(service_category, list):
            base_services = {'Cleaning', 'Child_care', 'Cooking', 'Caregiver', 'Laundry'}
            current_set = set(service_category)
            if base_services.issubset(current_set) or ('All-around' in current_set and len(current_set) > 1):
                data['service_category'] = ['All-around']

        now = timezone.now()
        start_time = data.get('start_time')
        end_time = data.get('end_time')

        if start_time and start_time <= now: 
            raise serializers.ValidationError({"start_time": "Start time must be in the future."})
        
        if end_time and end_time <= now:
            raise serializers.ValidationError({"end_time": "End time must be in the future."})

        if (start_time and end_time) and end_time <= start_time:
            raise serializers.ValidationError({"end_time": "End time must be strictly after the start time."})

        # Batas Kasambahay (RA 10361) statutory minimum wage enforcement for long-term services
        booking_type = data.get('booking_type') or (self.instance.booking_type if self.instance else None)
        daily_rate = data.get('daily_rate') if 'daily_rate' in data else (self.instance.daily_rate if self.instance else None)
        
        street = data.get('street') or (self.instance.street if self.instance else '')
        barangay = data.get('barangay') or (self.instance.barangay if self.instance else '')
        city = data.get('city') or (self.instance.city if self.instance else '')
        province = data.get('province') or (self.instance.province if self.instance else '')
        zip_code = data.get('zip_code') or (self.instance.zip_code if self.instance else '9000')
        data['zip_code'] = zip_code
        full_address = f"{street} {barangay} {city} {province} {zip_code}".strip()

        if daily_rate is not None:
            if not isinstance(daily_rate, Decimal):
                try:
                    daily_rate = Decimal(str(daily_rate))
                except Exception:
                    raise serializers.ValidationError({"daily_rate": "Daily rate must be a valid number."})

            if not daily_rate.is_finite() or daily_rate < Decimal('1.00'):
                raise serializers.ValidationError({"daily_rate": "Daily rate must be at least ₱1.00."})

            if daily_rate > Decimal('999999.99'):
                raise serializers.ValidationError({"daily_rate": "Daily rate cannot exceed ₱999,999.99."})

            if booking_type == 'long_term':
                min_wage = get_minimum_daily_wage('long_term', full_address)
                if daily_rate < min_wage:
                    approx_monthly = get_monthly_equivalent(min_wage)
                    raise serializers.ValidationError({
                        "daily_rate": (
                            f"Under Batas Kasambahay (RA 10361), the minimum daily wage for long-term "
                            f"domestic service in this region is ₱{min_wage:.2f}/day (approx. ₱{approx_monthly:,.2f}/month)."
                        )
                    })

        # Sanity check: Long-term domestic service duration cannot be under 24 hours
        if booking_type == 'long_term' and start_time and end_time:
            if (end_time - start_time).total_seconds() < 86400:
                raise serializers.ValidationError({
                    "booking_type": (
                        "Long-term bookings are for ongoing domestic employment lasting multiple days or months. "
                        "For tasks or shifts under 24 hours, please select Short-term."
                    )
                })

        return data


class BookingFeedSerializer(serializers.ModelSerializer):
    profile_link = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    poster_account_type = serializers.SerializerMethodField()
    full_address = serializers.ReadOnlyField()

    class Meta:
        model = tbl_booking
        fields = [
            'booking_id',
            'poster_id',
            'poster_account_type',
            'booking_type',
            'booking_status',
            'profile_link',
            'name',
            'region',
            'province',
            'city',
            'barangay',
            'street',
            'full_address',
            'floor_number',
            'zip_code',
            'service_category',
            'daily_rate',
            'special_instruction',
            'start_time',
            'end_time',
            'createdAt',
        ]

    def get_poster_account_type(self, obj):
        return getattr(obj.poster_id, 'account_type', 'User')
    
    def get_name(self, obj):
        first_name = obj.poster_id.first_name or ''
        middle_name = obj.poster_id.middle_name or ''
        last_name = obj.poster_id.last_name or ''
        full = f"{first_name} {middle_name} {last_name}".strip()
        return full if full else obj.poster_id.username
    
    def get_profile_link(self, obj):
        return get_signed_avatar(obj.poster_id)


class BookingDetailSerializer(serializers.ModelSerializer):
    poster = serializers.SerializerMethodField()
    assigned_partner = serializers.SerializerMethodField()
    has_reviewed = serializers.SerializerMethodField()
    proposals_count = serializers.SerializerMethodField()
    full_address = serializers.ReadOnlyField()
    cancel_requested_by = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()
    cancellation_deadline = serializers.SerializerMethodField()
    is_cancel_requested = serializers.SerializerMethodField()
    cancel_requested_by_me = serializers.SerializerMethodField()
    pending_cancel_approval = serializers.SerializerMethodField()

    class Meta:
        model = tbl_booking
        fields = [
            'booking_id',
            'booking_type',
            'booking_status',
            'service_category',
            'start_time',
            'end_time',
            'region',
            'province',
            'city',
            'barangay',
            'street',
            'full_address',
            'floor_number',
            'zip_code',
            'special_instruction',
            'daily_rate',
            'createdAt',
            'poster',
            'assigned_partner',
            'has_reviewed',
            'proposals_count',
            'cancel_requested_by',
            'cancel_requested_at',
            'cancellation_reason',
            'can_cancel',
            'cancellation_deadline',
            'is_cancel_requested',
            'cancel_requested_by_me',
            'pending_cancel_approval',
        ]

    def get_poster(self, obj):
        user = obj.poster_id
        first = user.first_name or ''
        last = user.last_name or ''
        full_name = f"{first} {last}".strip() or user.username
        
        avg_rating = tbl_review.objects.filter(reviewee_id=user).aggregate(avg=Avg('rating'))['avg']
        rating = round(float(avg_rating), 1) if avg_rating is not None else 5.0

        return {
            'id': str(user.id),
            'name': full_name,
            'account_type': user.account_type,
            'verification_status': user.verification_status,
            'profile_link': get_signed_avatar(user),
            'rating': rating,
            'contact_number': user.contact_number if user.contact_number else None,
        }

    def get_assigned_partner(self, obj):
        assignment = obj.assignments.select_related('accepter_id').first()
        if not assignment or not assignment.accepter_id:
            return None
        user = assignment.accepter_id
        first = user.first_name or ''
        last = user.last_name or ''
        full_name = f"{first} {last}".strip() or user.username
        
        avg_rating = tbl_review.objects.filter(reviewee_id=user).aggregate(avg=Avg('rating'))['avg']
        rating = round(float(avg_rating), 1) if avg_rating is not None else 5.0

        return {
            'id': str(user.id),
            'name': full_name,
            'account_type': user.account_type,
            'verification_status': user.verification_status,
            'profile_link': get_signed_avatar(user),
            'rating': rating,
            'contact_number': user.contact_number if user.contact_number else None,
            'accepted_at': assignment.accepted_at,
        }

    def get_has_reviewed(self, obj):
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return tbl_review.objects.filter(booking_id=obj, reviewer_id=request.user).exists()

    def get_proposals_count(self, obj):
        return obj.proposals.count()

    def get_cancel_requested_by(self, obj):
        if not obj.cancel_requested_by:
            return None
        u = obj.cancel_requested_by
        first = u.first_name or ''
        last = u.last_name or ''
        full_name = f"{first} {last}".strip() or u.username
        return {
            'id': str(u.id),
            'name': full_name,
            'account_type': u.account_type,
        }

    def get_cancellation_deadline(self, obj):
        if obj.booking_status in ['Accepted', 'InProgress']:
            assignment = obj.assignments.first()
            confirmed_at = assignment.accepted_at if (assignment and assignment.accepted_at) else obj.createdAt
            if confirmed_at:
                from datetime import timedelta
                return (confirmed_at + timedelta(hours=2)).isoformat()
        return None

    def get_can_cancel(self, obj):
        if obj.booking_status == 'Pending':
            return True
        if obj.booking_status in ['Accepted', 'InProgress']:
            assignment = obj.assignments.first()
            confirmed_at = assignment.accepted_at if (assignment and assignment.accepted_at) else obj.createdAt
            if confirmed_at:
                from django.utils import timezone
                from datetime import timedelta
                return timezone.now() <= (confirmed_at + timedelta(hours=2))
        return False

    def get_is_cancel_requested(self, obj):
        return bool(obj.cancel_requested_by)

    def get_cancel_requested_by_me(self, obj):
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return bool(obj.cancel_requested_by and obj.cancel_requested_by == request.user)

    def get_pending_cancel_approval(self, obj):
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            return False
        return bool(obj.cancel_requested_by and obj.cancel_requested_by != request.user)


class BookingProposalSerializer(serializers.ModelSerializer):
    proposer_name = serializers.SerializerMethodField()
    proposer_role = serializers.SerializerMethodField()
    proposer_avatar = serializers.SerializerMethodField()
    proposer_rating = serializers.SerializerMethodField()

    class Meta:
        model = tbl_booking_proposal
        fields = [
            'proposal_id',
            'booking_id',
            'proposer_id',
            'proposer_name',
            'proposer_role',
            'proposer_avatar',
            'proposer_rating',
            'proposed_rate',
            'message',
            'status',
            'createdAt',
        ]
        read_only_fields = ['proposal_id', 'proposer_id', 'status', 'createdAt']

    def get_proposer_name(self, obj):
        first = obj.proposer_id.first_name or ''
        last = obj.proposer_id.last_name or ''
        full_name = f"{first} {last}".strip()
        return full_name if full_name else obj.proposer_id.username

    def get_proposer_role(self, obj):
        return obj.proposer_id.account_type

    def get_proposer_avatar(self, obj):
        return get_signed_avatar(obj.proposer_id)

    def get_proposer_rating(self, obj):
        avg_rating = tbl_review.objects.filter(reviewee_id=obj.proposer_id).aggregate(avg=Avg('rating'))['avg']
        return round(float(avg_rating), 1) if avg_rating is not None else 5.0