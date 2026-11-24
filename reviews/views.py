from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle
from rest_framework.exceptions import Throttled, NotFound
from rest_framework.response import Response
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Avg
from core.utils import check_valid_uuid, get_signed_cloudinary_url
from .models import tbl_review
from .sentiment import analyze_feedback, SentimentUnavailable
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

    def replay(self, request, key):
        review = tbl_review.objects.filter(reviewer_id=request.user, submission_key=key).first()
        if review is None:
            return None
        req_rec = request.data.get('would_recommend')
        if (str(review.booking_id_id) != str(request.data.get('booking_id'))
                or str(review.rating) != str(request.data.get('rating'))
                or review.unstructured_feedback != str(request.data.get('unstructured_feedback', '')).strip()
                or (req_rec is not None and review.would_recommend is not None and bool(req_rec) != bool(review.would_recommend))):
            return Response({'detail': 'This submission key was already used for a different review.'}, status=400)
        return Response({'message': 'Review submitted successfully.', 'data': ReviewSerializer(review).data}, status=200)

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

        # Durable, reviewer-scoped replay survives process restarts and network retries.
        replay = self.replay(request, idempotency_key)
        if replay is not None:
            return replay
        serializer = self.get_serializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        try:
            analysis = analyze_feedback(serializer.validated_data['unstructured_feedback'])
        except SentimentUnavailable:
            return Response({
                'detail': 'We could not analyze your feedback right now. Your review has not been submitted. Please try again.',
                'code': 'sentiment_unavailable', 'retryable': True,
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        # Never hold a database lock while waiting on the external model.
        with transaction.atomic():
            User.objects.select_for_update().get(pk=request.user.pk)
            replay = self.replay(request, idempotency_key)
            if replay is not None:
                return replay
            serializer = self.get_serializer(data=request.data, context={'request': request})
            serializer.is_valid(raise_exception=True)
            review = serializer.save(nlp_sentiment=analysis['label'], sentiment_analysis=analysis,
                                     submission_key=idempotency_key)

        # In-App Notification Trigger (Tier 1-3)
        try:
            from notifications.services import send_in_app_notification
            reviewer_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
            send_in_app_notification(
                receiver=review.reviewee_id,
                sender=request.user,
                message=f"{reviewer_name} gave you a {review.rating}-star review: \"{review.unstructured_feedback[:50]}...\""
            )
        except Exception:
            pass

        response_data = {
            "message": "Review submitted successfully.",
            "data": ReviewSerializer(review).data
        }
        response_status = status.HTTP_201_CREATED

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

        # Profile image resolution (Optimized WebP)
        profile_url = get_signed_cloudinary_url(getattr(target_user, 'profile_link', None), as_avatar=True)

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


# ─────────────────────────────────────────────
# GET /api/v1/reviews/analytics/ (Tier 2-5)
# ─────────────────────────────────────────────

class ReviewAnalyticsView(generics.GenericAPIView):
    """
    Returns reputation & performance analytics for the authenticated user (e.g. Kasambahay).
    Includes total jobs completed, total reviews, average rating, sentiment breakdown,
    and 5-star to 1-star distribution.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ReviewListThrottle]

    def get(self, request, *args, **kwargs):
        current_user = request.user
        reviews = tbl_review.objects.filter(reviewee_id=current_user)
        total_reviews = reviews.count()

        avg_agg = reviews.aggregate(avg=Avg('rating'))['avg']
        average_rating = round(float(avg_agg), 2) if avg_agg is not None else 0.0

        sentiment_breakdown = {'Positive': 0, 'Neutral': 0, 'Negative': 0}
        rating_breakdown = {'5': 0, '4': 0, '3': 0, '2': 0, '1': 0}

        for row in reviews.values('nlp_sentiment', 'rating'):
            sentiment = row['nlp_sentiment']
            rating = str(row['rating'])
            if sentiment in sentiment_breakdown:
                sentiment_breakdown[sentiment] += 1
            if rating in rating_breakdown:
                rating_breakdown[rating] += 1

        positive_count = sentiment_breakdown['Positive']
        positive_pct = round((positive_count / total_reviews) * 100) if total_reviews > 0 else 100

        # Calculate completed jobs count from assignments and poster bookings
        from booking.models import tbl_booking, tbl_booking_assignment
        assigned_completed = tbl_booking_assignment.objects.filter(
            accepter_id=current_user,
            booking_id__booking_status='Completed'
        ).count()
        posted_completed = tbl_booking.objects.filter(
            poster_id=current_user,
            booking_status='Completed'
        ).count()
        total_jobs_completed = assigned_completed + posted_completed

        recent_reviews = ReviewSerializer(reviews.select_related('reviewer_id', 'booking_id').order_by('-createdAt')[:5], many=True).data

        return Response({
            "message": "Review analytics retrieved successfully.",
            "data": {
                "user_id": current_user.id,
                "user_name": f"{current_user.first_name} {current_user.last_name}".strip() or current_user.username,
                "account_type": current_user.account_type,
                "total_jobs_completed": total_jobs_completed,
                "total_reviews": total_reviews,
                "average_rating": average_rating,
                "positive_percentage": positive_pct,
                "sentiment_breakdown": sentiment_breakdown,
                "rating_breakdown": rating_breakdown,
                "recent_reviews": recent_reviews,
            }
        }, status=status.HTTP_200_OK)

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))

