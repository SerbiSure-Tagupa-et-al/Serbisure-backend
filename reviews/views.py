from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle
from rest_framework.exceptions import Throttled, NotFound
from rest_framework.response import Response
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.db.models import Avg
from core.utils import check_valid_uuid
from .models import tbl_review
from .serializers import (
    CreateReviewSerializer,
    ReviewSerializer,
    UserReviewSummarySerializer
)
import math
import cloudinary.utils

User = get_user_model()


# ─────────────────────────────────────────────
# Throttle Classes
# ─────────────────────────────────────────────

class CreateReviewThrottle(UserRateThrottle):
    scope = 'review_create'
    rate = '30/d'


class ReviewListThrottle(UserRateThrottle):
    scope = 'review_list'
    rate = '120/m'


# ─────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────

def _get_throttle_message(wait):
    """Returns a standardized throttle message based on wait time in seconds."""
    if wait > 3600:
        time_left = math.ceil(wait / 3600)
        return f"Too many requests. Please try again in {time_left} hours."
    return f"Too many requests. Please try again in {math.ceil(wait / 60)} minutes."


# ─────────────────────────────────────────────
# POST /api/v1/reviews/create/
# ─────────────────────────────────────────────

class CreateReviewView(generics.CreateAPIView):
    """
    Submits a review for a completed booking.
    Requires a verified user and a valid UUIDv4 Idempotency-Key header.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [CreateReviewThrottle]
    serializer_class = CreateReviewSerializer

    def create(self, request, *args, **kwargs):
        # Step 1: Verification guard
        if getattr(request.user, 'verification_status', None) != "Verified":
            return Response(
                {"detail": "Only verified users can submit reviews."},
                status=status.HTTP_403_FORBIDDEN
            )

        # Step 2: Validate Idempotency-Key header
        idempotency_key = request.headers.get('Idempotency-Key')
        if not idempotency_key or not check_valid_uuid(idempotency_key):
            return Response(
                {"detail": "The Idempotency-Key header is required and must be a valid UUID v4."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Step 3: Check cache — prevent duplicate on network retry
        cache_key = f'review_create_{idempotency_key}'
        cached_response = cache.get(cache_key)
        if cached_response:
            return Response(cached_response['data'], status=cached_response['status'])

        # Step 4: Validate and save review
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        review = serializer.save()

        response_data = {
            "message": "Review submitted successfully.",
            "data": ReviewSerializer(review).data
        }
        response_status = status.HTTP_201_CREATED

        # Step 7: Cache the success response for 24 hours
        cache.set(
            cache_key,
            {'data': response_data, 'status': response_status},
            timeout=86400  # 24 hours
        )

        return Response(response_data, status=response_status)

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))


# ─────────────────────────────────────────────
# GET /api/v1/reviews/received/
# ─────────────────────────────────────────────

class ReceivedReviewsView(generics.ListAPIView):
    """
    Retrieves all reviews received by the authenticated user.
    Returns an empty list [] if no reviews exist (never raises 500).
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ReviewListThrottle]
    serializer_class = ReviewSerializer

    def get_queryset(self):
        return tbl_review.objects.filter(
            reviewee_id=self.request.user
        ).select_related('reviewer_id', 'reviewee_id', 'booking_id').order_by('-createdAt')

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))


# ─────────────────────────────────────────────
# GET /api/v1/reviews/given/
# ─────────────────────────────────────────────

class GivenReviewsView(generics.ListAPIView):
    """
    Retrieves all reviews written and submitted by the authenticated user.
    Returns an empty list [] if no reviews exist (never raises 500).
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ReviewListThrottle]
    serializer_class = ReviewSerializer

    def get_queryset(self):
        return tbl_review.objects.filter(
            reviewer_id=self.request.user
        ).select_related('reviewer_id', 'reviewee_id', 'booking_id').order_by('-createdAt')

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))


# ─────────────────────────────────────────────
# GET /api/v1/reviews/summary/<uuid:user_id>/
# ─────────────────────────────────────────────

class ReviewSummaryView(generics.GenericAPIView):
    """
    Returns an aggregated review summary for a specific user.
    Uses a single annotated query for sentiment/rating breakdown instead of 7 queries.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ReviewListThrottle]
    serializer_class = UserReviewSummarySerializer

    def get(self, request, user_id, *args, **kwargs):
        # user_id is already a UUID object here (Django's <uuid:> URL converter validates it)
        try:
            target_user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_404_NOT_FOUND
            )

        reviews = tbl_review.objects.filter(reviewee_id=target_user)
        total_reviews = reviews.count()

        avg_agg = reviews.aggregate(avg=Avg('rating'))['avg']
        average_rating = round(float(avg_agg), 2) if avg_agg is not None else 0.0

        # Single-pass aggregation instead of 7 separate DB queries
        sentiment_breakdown = {'Positive': 0, 'Neutral': 0, 'Negative': 0}
        rating_breakdown = {'5': 0, '4': 0, '3': 0, '2': 0, '1': 0}

        for row in reviews.values('nlp_sentiment', 'rating'):
            sentiment = row['nlp_sentiment']
            rating = str(row['rating'])
            if sentiment in sentiment_breakdown:
                sentiment_breakdown[sentiment] += 1
            if rating in rating_breakdown:
                rating_breakdown[rating] += 1

        # Profile image resolution
        profile_url = None
        public_id = getattr(target_user, 'profile_link', None)
        if public_id:
            try:
                profile_url, _ = cloudinary.utils.cloudinary_url(
                    public_id,
                    type="authenticated",
                    sign_url=True
                )
            except Exception:
                profile_url = None

        first = target_user.first_name or ''
        last = target_user.last_name or ''
        user_name = f"{first} {last}".strip() or target_user.username

        summary_data = {
            'user_id': target_user.id,
            'user_name': user_name,
            'account_type': target_user.account_type,
            'profile_link': profile_url,
            'average_rating': average_rating,
            'total_reviews': total_reviews,
            'sentiment_breakdown': sentiment_breakdown,
            'rating_breakdown': rating_breakdown,
        }

        serializer = self.get_serializer(summary_data)
        return Response({
            "message": "Review summary retrieved successfully.",
            "data": serializer.data
        }, status=status.HTTP_200_OK)

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))


# ─────────────────────────────────────────────
# GET /api/v1/reviews/user/<uuid:user_id>/
# ─────────────────────────────────────────────

class UserReviewsView(generics.ListAPIView):
    """
    Retrieves the public list of all reviews received by a target user.
    Returns [] if the user exists but has no reviews.
    Returns 404 if the user_id does not match any user.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ReviewListThrottle]
    serializer_class = ReviewSerializer

    def get_queryset(self):
        # user_id is already UUID-validated by Django's <uuid:> URL converter
        user_id = self.kwargs.get('user_id')

        # Guard: ensure target user actually exists (prevents returning empty list for phantom UUIDs)
        if not User.objects.filter(id=user_id).exists():
            raise NotFound(detail="User not found.")

        return tbl_review.objects.filter(
            reviewee_id=user_id
        ).select_related('reviewer_id', 'reviewee_id', 'booking_id').order_by('-createdAt')

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))
