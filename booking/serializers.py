from rest_framework import serializers
from .models import tbl_booking, tbl_booking_proposal, tbl_saved_post
from .wage_policy import validate_rate, DOLE_CONTRACT_PDF_URL
from django.utils import timezone
from .services import PH_TIME
from reviews.models import tbl_review
from django.db.models import Avg
from core.utils import get_signed_cloudinary_url
from .availability import post_status, schedule_expired


class BookingCancellationSerializer(serializers.Serializer):
    pending_only = serializers.BooleanField(required=False, default=False)
    reason = serializers.CharField(max_length=255, allow_blank=False,
        error_messages={'required': 'Choose a cancellation reason.', 'blank': 'Choose a cancellation reason.'})
    expected_points = serializers.IntegerField(min_value=0, max_value=2, required=False)
    action = serializers.ChoiceField(choices=['cancel', 'request', 'reject'], required=False)


class ChatOfferContextSerializer(serializers.Serializer):
    recipient_id = serializers.UUIDField(error_messages={
        'required': 'Choose a recipient before sending this offer.',
        'invalid': 'The recipient is invalid. Reopen their profile and try again.',
    })


def get_signed_avatar(user):
    if not user:
        return None
    public_id = getattr(user, 'profile_link', None)
    return get_signed_cloudinary_url(public_id, as_avatar=True)


class RepostScheduleSerializer(serializers.Serializer):
    start_time = serializers.DateTimeField()
    end_time = serializers.DateTimeField(required=False, allow_null=True)
    daily_start_time = serializers.TimeField()
    daily_end_time = serializers.TimeField()
    work_days = serializers.ListField(child=serializers.ChoiceField(choices=['M', 'T', 'W', 'Th', 'F', 'S', 'Su']), min_length=1)


class PostStateSerializerMixin:
    def get_schedule_expired(self, obj):
        return schedule_expired(obj)

    def get_post_status(self, obj):
        return post_status(obj)

    def get_can_repost(self, obj):
        request = self.context.get('request')
        return bool(request and request.user.pk == obj.poster_id_id and not obj.recipient_id_id
                    and post_status(obj) == 'Available' and schedule_expired(obj))


class BookingSerializer(serializers.ModelSerializer):
    work_setup = serializers.ChoiceField(choices=tbl_booking.WORK_SETUP_CHOICES, required=True)
    zip_code = serializers.CharField(max_length=4, required=False, default='', allow_blank=True)
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
            'work_setup',
            'work_days',
            'daily_start_time',
            'daily_end_time',
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
            'rate_amount',
            'rate_period',
            'rate_needs_review',
            'poster_id',
            'createdAt'
        ]
        read_only_fields = ['booking_id', 'booking_status', 'poster_id', 'createdAt', 'rate_needs_review']

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

        days = data.get('work_days')
        if days and len(set(days)) != len(days):
            raise serializers.ValidationError({'work_days': 'Select each working day only once.'})
        daily_start, daily_end = data.get('daily_start_time'), data.get('daily_end_time')
        if (daily_start is None) != (daily_end is None):
            raise serializers.ValidationError({'daily_end_time': 'Provide both daily working times.'})
        if daily_start and daily_end and daily_start >= daily_end:
            raise serializers.ValidationError({'daily_end_time': 'The end of the shift must be after its start.'})

        now = timezone.now()
        start_time = data.get('start_time')
        end_time = data.get('end_time')

        # Public posts and chat offers use the same Philippine schedule validation.
        for field, clock in (('start_time', daily_start), ('end_time', daily_end)):
            stamp = data.get(field)
            if stamp and clock and stamp.astimezone(PH_TIME).time().replace(tzinfo=None) != clock:
                raise serializers.ValidationError({field: 'The schedule must match the selected working time in the Philippines.'})
        if start_time and days:
            start_day = ['M', 'T', 'W', 'Th', 'F', 'S', 'Su'][start_time.astimezone(PH_TIME).weekday()]
            short_range = (data.get('booking_type') == 'short_term' and end_time
                and end_time.astimezone(PH_TIME).date() > start_time.astimezone(PH_TIME).date())
            if short_range:
                day_span = (end_time.astimezone(PH_TIME).date() - start_time.astimezone(PH_TIME).date()).days
                weekdays = ['M', 'T', 'W', 'Th', 'F', 'S', 'Su']
                first = start_time.astimezone(PH_TIME).weekday()
                if not any(weekdays[(first + offset) % 7] in days for offset in range(min(day_span + 1, 7))):
                    raise serializers.ValidationError({'work_days': 'Select a working day within the date range.'})
            elif start_day not in days:
                raise serializers.ValidationError({'work_days': 'The start date must fall on a selected working day.'})

        if start_time and start_time <= now: 
            raise serializers.ValidationError({"start_time": "Start time must be in the future."})
        
        if end_time and end_time <= now:
            raise serializers.ValidationError({"end_time": "End time must be in the future."})

        if (start_time and end_time) and end_time <= start_time:
            raise serializers.ValidationError({"end_time": "End time must be strictly after the start time."})

        booking_type = data.get('booking_type') or (self.instance.booking_type if self.instance else None)
        amount = data.get('rate_amount', self.instance.rate_amount if self.instance else None)
        region = data.get('region', self.instance.region if self.instance else '')
        address = ' '.join(str(data.get(key, getattr(self.instance, key, '') if self.instance else '') or '')
                           for key in ('street', 'barangay', 'city', 'province', 'zip_code'))
        validate_rate(amount, booking_type, region, address, period=data.get('rate_period'))
        if data.get('rate_period') is None:
            raise serializers.ValidationError({'rate_period': 'Specify day for short-term or month for long-term.'})

        # Platform minimum uses Philippine calendar dates, not shift hours.
        if booking_type == 'long_term' and start_time and end_time:
            if (end_time.astimezone(PH_TIME).date() - start_time.astimezone(PH_TIME).date()).days < 30:
                raise serializers.ValidationError({
                    'end_time': 'SerbiSure requires at least 30 days for long-term posts. Choose a later end date.'
                })

        return data


class BookingFeedSerializer(PostStateSerializerMixin, serializers.ModelSerializer):
    schedule_expired = serializers.SerializerMethodField()
    post_status = serializers.SerializerMethodField()
    can_repost = serializers.SerializerMethodField()
    profile_link = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    poster_account_type = serializers.SerializerMethodField()
    poster_verification_status = serializers.CharField(source='poster_id.verification_status', read_only=True)
    full_address = serializers.ReadOnlyField()

    class Meta:
        model = tbl_booking
        fields = [
            'booking_id',
            'poster_id',
            'poster_account_type',
            'post_status',
            'schedule_expired',
            'can_repost',
            'poster_verification_status',
            'booking_type',
            'work_setup',
            'work_days',
            'daily_start_time',
            'daily_end_time',
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
            'rate_amount',
            'rate_period',
            'rate_needs_review',
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


class ApplicationPostSerializer(BookingFeedSerializer):
    application_status = serializers.SerializerMethodField()

    class Meta(BookingFeedSerializer.Meta):
        fields = BookingFeedSerializer.Meta.fields + ['application_status']

    def get_application_status(self, obj):
        proposal = obj.viewer_application
        if proposal.status == 'Accepted':
            return 'accepted'
        if proposal.status == 'Withdrawn':
            return 'withdrawn'
        status = post_status(obj)
        if status == 'Cancelled':
            return 'cancelled'
        if proposal.status == 'Rejected' or status in ('Booked', 'Ongoing', 'Completed'):
            return 'not_selected'
        return 'applied'


class PublicPendingPostSerializer(BookingFeedSerializer):
    viewer_proposal = serializers.SerializerMethodField()

    class Meta(BookingFeedSerializer.Meta):
        fields = BookingFeedSerializer.Meta.fields + ['viewer_proposal']

    def get_viewer_proposal(self, obj):
        proposals = getattr(obj, 'viewer_proposals', [])
        if not proposals:
            return None
        proposal = proposals[0]
        return {'proposal_id': str(proposal.proposal_id), 'status': proposal.status}


class BookingDetailSerializer(PostStateSerializerMixin, serializers.ModelSerializer):
    pass_url = serializers.SerializerMethodField()
    schedule_expired = serializers.SerializerMethodField()
    post_status = serializers.SerializerMethodField()
    can_repost = serializers.SerializerMethodField()
    poster = serializers.SerializerMethodField()
    assigned_partner = serializers.SerializerMethodField()
    recipient = serializers.SerializerMethodField()
    has_reviewed = serializers.SerializerMethodField()
    proposals_count = serializers.SerializerMethodField()
    full_address = serializers.ReadOnlyField()
    contract_summary = serializers.SerializerMethodField()

    class Meta:
        model = tbl_booking
        fields = [
            'booking_id',
            'pass_url',
            'booking_type',
            'work_setup',
            'work_days',
            'daily_start_time',
            'daily_end_time',
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
            'rate_amount',
            'rate_period',
            'rate_needs_review',
            'createdAt',
            'recipient_id',
            'recipient',
            'poster',
            'assigned_partner',
            'has_reviewed',
            'proposals_count',
            'started_at',
            'completed_at',
            'contract_summary',
            'post_status',
            'schedule_expired',
            'can_repost',
            'cancellation_reason',
            'cancelled_by',
            'cancelled_at',
            'cancellation_penalty_points',
        ]

    def get_pass_url(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return None
        person_id = request.user.pk
        if person_id not in (obj.poster_id_id, obj.recipient_id_id) and not obj.assignments.filter(accepter_id_id=person_id).exists():
            return None
        from .gate_pass import gate_pass_url
        return gate_pass_url(request, obj.pk)

    def get_contract_summary(self, obj):
        request = self.context.get('request')
        assignment = obj.assignments.select_related('accepter_id').first()
        if (obj.booking_type != 'long_term' or obj.booking_status not in ['Accepted', 'InProgress', 'Completed']
                or not request or not assignment
                or request.user.pk not in (obj.poster_id_id, assignment.accepter_id_id)):
            return None
        return {
            'booking_id': str(obj.pk),
            'service_category': obj.service_category,
            'work_setup': obj.work_setup,
            'work_days': obj.work_days,
            'daily_start_time': obj.daily_start_time.isoformat() if obj.daily_start_time else None,
            'daily_end_time': obj.daily_end_time.isoformat() if obj.daily_end_time else None,
            'rate_amount': None if obj.rate_needs_review else str(obj.rate_amount),
            'rate_needs_review': obj.rate_needs_review,
            'rate_period': obj.rate_period,
            'start_date': obj.start_time.isoformat(),
            'end_date': obj.end_time.isoformat() if obj.end_time else None,
            'location': obj.full_address,
            'contract_pdf_url': request.build_absolute_uri('/api/v1/booking/contract-template/'),
            'source_url': DOLE_CONTRACT_PDF_URL,
            'notice': 'Print and complete the DOLE form, have both parties sign, and submit a physical copy to your local Barangay Kasambahay Desk. The employer is responsible for registration under RA 10361 Section 17. SerbiSure does not digitally sign or register this contract.',
        }

    def get_poster(self, obj):
        user = obj.poster_id
        first = user.first_name or ''
        last = user.last_name or ''
        full_name = f"{first} {last}".strip() or user.username
        
        avg_rating = tbl_review.objects.filter(reviewee_id=user).aggregate(avg=Avg('rating'))['avg']
        rating = round(float(avg_rating), 1) if avg_rating is not None else 0.0

        return {
            'id': str(user.id),
            'name': full_name,
            'account_type': user.account_type,
            'verification_status': user.verification_status,
            'profile_link': get_signed_avatar(user),
            'rating': rating,
            'contact_number': user.contact_number if user.contact_number else None,
        }

    def get_recipient(self, obj):
        user = obj.recipient_id
        if not user:
            return None
        return {'id': str(user.pk), 'name': (user.first_name + ' ' + user.last_name).strip() or user.username,
                'account_type': user.account_type, 'verification_status': user.verification_status,
                'profile_link': get_signed_avatar(user),
                'rating': round(float(tbl_review.objects.filter(reviewee_id=user).aggregate(avg=Avg('rating'))['avg'] or 0), 1)}

    def get_assigned_partner(self, obj):
        assignment = obj.assignments.select_related('accepter_id').first()
        if not assignment or not assignment.accepter_id:
            return None
        user = assignment.accepter_id
        first = user.first_name or ''
        last = user.last_name or ''
        full_name = f"{first} {last}".strip() or user.username
        
        avg_rating = tbl_review.objects.filter(reviewee_id=user).aggregate(avg=Avg('rating'))['avg']
        rating = round(float(avg_rating), 1) if avg_rating is not None else 0.0

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


class BookingProposalSerializer(serializers.ModelSerializer):
    proposer_name = serializers.SerializerMethodField()
    proposer_role = serializers.SerializerMethodField()
    proposer_avatar = serializers.SerializerMethodField()
    proposer_rating = serializers.SerializerMethodField()
    rate_period = serializers.CharField(source='booking_id.rate_period', read_only=True)

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
            'rate_period',
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
        return round(float(avg_rating), 1) if avg_rating is not None else 0.0


class SavedPostItemSerializer(serializers.ModelSerializer):
    """
    Serializes a tbl_saved_post record with full embedded booking details 
    formatted to match the mobile feed directly.
    """
    saved_id = serializers.UUIDField(read_only=True)
    saved_at = serializers.DateTimeField(source='created_at', read_only=True)
    booking = BookingFeedSerializer(read_only=True)

    class Meta:
        model = tbl_saved_post
        fields = ['saved_id', 'saved_at', 'booking']

    def to_representation(self, instance):
        if not instance.booking:
            return None
        data = BookingFeedSerializer(instance.booking).data
        data['saved_id'] = str(instance.saved_id)
        data['saved_at'] = instance.created_at.isoformat()
        return data
