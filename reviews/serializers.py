from rest_framework import serializers
from .models import tbl_review
from booking.models import tbl_booking, tbl_booking_assignment
import cloudinary.utils


class CreateReviewSerializer(serializers.ModelSerializer):
    rating = serializers.IntegerField(min_value=1, max_value=5)
    nlp_sentiment = serializers.ChoiceField(choices=tbl_review.NLP_TYPE_CHOICES)
    # min_length=10 prevents meaningless 1-character submissions
    unstructured_feedback = serializers.CharField(min_length=10, max_length=1000, allow_blank=False)

    class Meta:
        model = tbl_review
        fields = ['booking_id', 'rating', 'unstructured_feedback', 'nlp_sentiment']
        # reviewer_id and reviewee_id are NOT listed here — they are injected via
        # serializer.save(reviewer_id=..., reviewee_id=...) in the view, which is
        # the standard DRF pattern for server-set fields.
        read_only_fields = ['review_id', 'createdAt']

    def validate(self, data):
        request = self.context.get('request')
        if not request or not request.user or not request.user.is_authenticated:
            raise serializers.ValidationError({"detail": "Authentication is required to review a booking."})

        user = request.user
        booking = data.get('booking_id')

        # 1. Booking must be in Completed status
        if booking.booking_status != 'Completed':
            raise serializers.ValidationError({
                "booking_id": "You can only submit a review for bookings that are marked as 'Completed'."
            })

        # 2. Retrieve the booking assignment to identify the worker/accepter
        assignment = tbl_booking_assignment.objects.filter(booking_id=booking).select_related('accepter_id').first()
        if not assignment or not assignment.accepter_id:
            raise serializers.ValidationError({
                "booking_id": "This booking does not have an assigned worker to review."
            })

        poster = booking.poster_id
        accepter = assignment.accepter_id

        # 3. Ensure the reviewer is a legitimate participant of this booking
        if user.id != poster.id and user.id != accepter.id:
            raise serializers.ValidationError({
                "detail": "You are not an authorized participant (poster or accepter) of this booking."
            })

        # 4. Automatically derive reviewee (the OTHER participant)
        reviewee = accepter if user.id == poster.id else poster

        # 5. Guard against reviewing self (edge case: if poster == accepter somehow)
        if user.id == reviewee.id:
            raise serializers.ValidationError({
                "detail": "You cannot review yourself."
            })

        # 6. Check if reviewer already reviewed this booking (pre-DB constraint check)
        if tbl_review.objects.filter(booking_id=booking, reviewer_id=user).exists():
            raise serializers.ValidationError({
                "detail": "You have already submitted a review for this booking."
            })

        # Set derived participants directly in validated data
        data['reviewer_id'] = user
        data['reviewee_id'] = reviewee

        return data

    def create(self, validated_data):
        return tbl_review.objects.create(**validated_data)


class ReviewSerializer(serializers.ModelSerializer):
    reviewer_name = serializers.SerializerMethodField()
    reviewer_profile_link = serializers.SerializerMethodField()
    reviewer_account_type = serializers.CharField(source='reviewer_id.account_type', read_only=True)

    reviewee_name = serializers.SerializerMethodField()
    reviewee_profile_link = serializers.SerializerMethodField()
    reviewee_account_type = serializers.CharField(source='reviewee_id.account_type', read_only=True)

    booking_service_category = serializers.SerializerMethodField()

    class Meta:
        model = tbl_review
        fields = [
            'review_id',
            'booking_id',
            'booking_service_category',
            'reviewer_id',
            'reviewer_name',
            'reviewer_account_type',
            'reviewer_profile_link',
            'reviewee_id',
            'reviewee_name',
            'reviewee_account_type',
            'reviewee_profile_link',
            'rating',
            'unstructured_feedback',
            'nlp_sentiment',
            'createdAt'
        ]

    def _get_signed_profile_link(self, user):
        public_id = getattr(user, 'profile_link', None)
        if not public_id:
            return None
        try:
            url, _ = cloudinary.utils.cloudinary_url(
                public_id,
                type="authenticated",
                sign_url=True
            )
            return url
        except Exception:
            return None

    def get_reviewer_name(self, obj):
        first = obj.reviewer_id.first_name or ''
        last = obj.reviewer_id.last_name or ''
        full_name = f"{first} {last}".strip()
        return full_name if full_name else obj.reviewer_id.username

    def get_reviewer_profile_link(self, obj):
        return self._get_signed_profile_link(obj.reviewer_id)

    def get_reviewee_name(self, obj):
        first = obj.reviewee_id.first_name or ''
        last = obj.reviewee_id.last_name or ''
        full_name = f"{first} {last}".strip()
        return full_name if full_name else obj.reviewee_id.username

    def get_reviewee_profile_link(self, obj):
        return self._get_signed_profile_link(obj.reviewee_id)

    def get_booking_service_category(self, obj):
        if obj.booking_id:
            return obj.booking_id.service_category
        return []


class UserReviewSummarySerializer(serializers.Serializer):
    user_id = serializers.UUIDField()
    user_name = serializers.CharField()
    account_type = serializers.CharField()
    # URLField correctly validates Cloudinary signed URLs; allow_null for users with no profile pic
    profile_link = serializers.URLField(allow_null=True)
    average_rating = serializers.FloatField()
    total_reviews = serializers.IntegerField()
    sentiment_breakdown = serializers.DictField()
    rating_breakdown = serializers.DictField()