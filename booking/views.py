from realtime.events import publish
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.throttling import UserRateThrottle
from rest_framework.exceptions import Throttled, ValidationError, NotFound, PermissionDenied
from core.utils import check_valid_uuid
from rest_framework import status
from rest_framework.response import Response
from rest_framework import generics
from rest_framework.views import APIView
from .serializers import (
    BookingSerializer,
    BookingFeedSerializer,
    PublicPendingPostSerializer,
    BookingDetailSerializer,
    BookingProposalSerializer,
    SavedPostItemSerializer
)
from .serializers import BookingCancellationSerializer
from .cancellation import cancel_booking, cancellation_preview, SAFETY_REASON
from accounts.penalties import assert_booking_unrestricted, penalty_status
from .models import tbl_booking, tbl_booking_proposal, tbl_saved_post
from reviews.models import tbl_review
from django.core.cache import cache
from django.db.models import Q, Avg, Prefetch
from decimal import Decimal, InvalidOperation
from django.contrib.auth import get_user_model
from notifications.services import send_in_app_notification
from .wage_policy import get_minimum_wage_info, validate_rate
from .services import accept_engagement, create_proposal
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from uuid import UUID
from .availability import available_public_posts, with_post_state
from .serializers import ApplicationPostSerializer, RepostScheduleSerializer
from .reposting import repost_public_post
import math 

User = get_user_model()


class BookingThrottle(UserRateThrottle):
    rate = '50/d'


class BookingView(generics.CreateAPIView):
    throttle_classes = [BookingThrottle]
    serializer_class = BookingSerializer
    queryset = tbl_booking.objects.all()
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        request.user = User.objects.select_for_update().get(pk=request.user.pk)
        assert_booking_unrestricted(request.user)
        if request.user.account_type not in ("Homeowner", "Kasambahay") or request.user.verification_status != "Verified":
            return Response({
                "code": "account_not_verified",
                "detail": "Account verification is required before posting. Please verify your account to keep our community safe and trusted.",
                "verification_status": request.user.verification_status
            }, status=status.HTTP_403_FORBIDDEN)

        idempotency_key = request.headers.get('Idempotency-Key')
        if not idempotency_key or not check_valid_uuid(idempotency_key):
            return Response({"detail": "The Idempotency-Key header is required and must be a valid UUID v4."},
                            status=status.HTTP_400_BAD_REQUEST)

        cache_key = f'booking_post_{request.user.pk}_{idempotency_key}'
        cached_response = cache.get(cache_key)
        if cached_response:
            return Response(cached_response['data'], status=cached_response['status'])
        
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid(raise_exception=True):
            booking = serializer.save(poster_id=request.user)

            response_data = {
                "message": "Booking posted successfully",
                "data": BookingDetailSerializer(booking, context={'request': request}).data
            }
            response_status = status.HTTP_201_CREATED

            if idempotency_key:
                cache.set(
                    cache_key,
                    {'data': response_data, 'status': response_status},
                    timeout=86400
                )

            return Response(response_data, status=response_status)

        return super().create(request, *args, **kwargs)
    
    def throttled(self, request, wait):
        if wait > 3600:
            time_left = math.ceil(wait / 3600)
            custom_message = f"Too many attempts. Please try again in {time_left} hours."
        else:
            custom_message = f"Too many attempts. Please try again in {math.ceil(wait / 60)} minutes"
        raise Throttled(detail=custom_message)


class BookingFeedView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = BookingFeedSerializer

    def get_queryset(self):
        current_user = self.request.user
        queryset = available_public_posts(tbl_booking.objects.select_related('poster_id'))

        if current_user.account_type == 'Homeowner':
            queryset = queryset.filter(poster_id__account_type='Kasambahay')
        elif current_user.account_type == 'Kasambahay':
            queryset = queryset.filter(poster_id__account_type='Homeowner')

        params = self.request.query_params

        # 1. Service Category filter
        category_param = params.get('category')
        if category_param:
            raw_cats = [c.strip() for c in category_param.split(',') if c.strip()]
            if raw_cats and 'All' not in raw_cats and 'all' not in raw_cats:
                cat_map = {
                    'cleaning': 'Cleaning',
                    'child_care': 'Child_care',
                    'child care': 'Child_care',
                    'cooking': 'Cooking',
                    'caregiver': 'Caregiver',
                    'laundry': 'Laundry',
                    'all-around': 'All-around',
                    'all around': 'All-around',
                }
                mapped_cats = [cat_map.get(c.lower(), c) for c in raw_cats]
                # If filtering for a specific domestic service, also include All-around postings
                if any(c in mapped_cats for c in ['Cleaning', 'Child_care', 'Cooking', 'Caregiver', 'Laundry']):
                    if 'All-around' not in mapped_cats:
                        mapped_cats.append('All-around')
                try:
                    queryset = queryset.filter(service_category__overlap=mapped_cats)
                except Exception:
                    cat_q = Q()
                    for cat in mapped_cats:
                        cat_q |= Q(service_category__icontains=cat)
                    queryset = queryset.filter(cat_q)

        # 2. Booking type filter
        bt_param = params.get('booking_type')
        if bt_param:
            bt_norm = bt_param.lower().replace('-', '_').strip()
            if bt_norm in ['short_term', 'part_time', 'parttime']:
                queryset = queryset.filter(booking_type='short_term')
            elif bt_norm == 'long_term':
                queryset = queryset.filter(booking_type='long_term')
            else:
                raise ValidationError({'booking_type': 'Choose short_term or long_term; filter living setup with work_setup.'})

        setup_param = params.get('work_setup')
        if setup_param:
            if setup_param not in ('stay_in', 'stay_out'):
                raise ValidationError({'work_setup': 'Choose stay_in or stay_out.'})
            queryset = queryset.filter(work_setup=setup_param)

        if params.get('max_rate') or params.get('min_rate') or params.get('rate_period'):
            period = params.get('rate_period')
            if period not in ('day', 'month'):
                raise ValidationError({'rate_period': 'Choose day or month when filtering or sorting compensation.'})
            queryset = queryset.filter(rate_period=period)

        # 3. Max & Min daily rate
        max_rate = params.get('max_rate')
        if max_rate:
            try:
                queryset = queryset.filter(rate_amount__lte=Decimal(str(max_rate)))
            except (InvalidOperation, ValueError, TypeError):
                pass

        min_rate = params.get('min_rate')
        if min_rate:
            try:
                queryset = queryset.filter(rate_amount__gte=Decimal(str(min_rate)))
            except (InvalidOperation, ValueError, TypeError):
                pass

        # 4. Location / Barangay / City filters
        barangay_param = params.get('barangay')
        if barangay_param and barangay_param.upper() not in ['ALL', 'ALL BARANGAYS']:
            b_norm = barangay_param.strip()
            queryset = queryset.filter(
                Q(barangay__iexact=b_norm) |
                Q(barangay__icontains=b_norm) |
                Q(street__icontains=b_norm)
            )

        city_param = params.get('city')
        if city_param and city_param.upper() not in ['ALL', 'ALL CITIES']:
            c_norm = city_param.strip()
            queryset = queryset.filter(
                Q(city__iexact=c_norm) |
                Q(city__icontains=c_norm)
            )

        location = params.get('location')
        if location and location.strip():
            loc = location.strip()
            queryset = queryset.filter(
                Q(barangay__icontains=loc) |
                Q(city__icontains=loc) |
                Q(province__icontains=loc) |
                Q(region__icontains=loc) |
                Q(street__icontains=loc)
            )

        # 5. Search keyword
        search_kw = params.get('search') or params.get('q')
        if search_kw and search_kw.strip():
            kw = search_kw.strip()
            queryset = queryset.filter(
                Q(barangay__icontains=kw) |
                Q(city__icontains=kw) |
                Q(street__icontains=kw) |
                Q(province__icontains=kw) |
                Q(region__icontains=kw) |
                Q(special_instruction__icontains=kw) |
                Q(poster_id__first_name__icontains=kw) |
                Q(poster_id__last_name__icontains=kw)
            )

        # 6. Sorting
        queryset = queryset.order_by('-createdAt', '-booking_id')

        return queryset


class PublicPendingPostsView(generics.ListAPIView):
    """Open posts by one active profile, with only the viewer's latest proposal."""
    permission_classes = [IsAuthenticated]
    serializer_class = PublicPendingPostSerializer
    pagination_class = None

    def get_queryset(self):
        poster_id = self.kwargs['user_id']
        if not User.objects.filter(id=poster_id, is_active=True,
                                   account_type__in=['Homeowner', 'Kasambahay']).exists():
            raise NotFound('Profile not found.')
        return available_public_posts(tbl_booking.objects.filter(poster_id=poster_id)).select_related('poster_id').prefetch_related(Prefetch(
            'proposals',
            queryset=tbl_booking_proposal.objects.filter(
                proposer_id=self.request.user
            ).order_by('-createdAt'),
            to_attr='viewer_proposals',
        )).order_by('-createdAt')


class BookingDetailView(generics.RetrieveAPIView):
    """
    Retrieve full details of a specific booking by UUID (Tier 1-1).
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BookingDetailSerializer
    lookup_field = 'booking_id'

    def get_queryset(self):
        user = self.request.user
        return tbl_booking.objects.select_related('poster_id').filter(
            Q(recipient_id__isnull=True) | Q(poster_id=user) | Q(recipient_id=user))


class BookingAcceptView(APIView):
    """
    Accepts a pending booking (Tier 1-1).
    - Can be accepted by the opposite party (e.g. Kasambahay accepting Homeowner's post).
    - Sets booking_status to 'Accepted'.
    - Creates or updates tbl_booking_assignment.
    - Sends an in-app notification to the poster.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, booking_id):
        booking, accepter = accept_engagement(booking_id, request.user)

        # Send in-app notification to the poster
        accepter_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
        cats = ", ".join(booking.service_category) if isinstance(booking.service_category, list) else str(booking.service_category)
        send_in_app_notification(
            receiver=booking.poster_id,
            sender=request.user,
            message=f"{accepter_name} has accepted your booking request for {cats}!"
        )

        return Response({
            'message': 'Booking accepted successfully.',
            'booking': BookingDetailSerializer(booking, context={'request': request}).data
        }, status=status.HTTP_200_OK)


class BookingStartView(APIView):
    """
    Marks an 'Accepted' booking as 'InProgress' (Tier 1-1).
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, booking_id):
        try:
            booking = tbl_booking.objects.select_for_update().get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return Response({'error': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        assignment = booking.assignments.select_related('accepter_id').first()
        is_accepter = assignment and assignment.accepter_id == request.user
        is_poster = booking.poster_id == request.user

        if not (is_poster or is_accepter):
            return Response({'error': 'You are not authorized to update this booking.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.booking_status != 'Accepted' and not (booking.booking_status == 'InProgress' and booking.started_at is None):
            return Response({'error': f'Booking must be in Accepted status to start. Current: {booking.booking_status}.'},
                            status=status.HTTP_400_BAD_REQUEST)

        if timezone.now() < booking.start_time:
            return Response({'error': 'Work cannot start before the agreed start date.'}, status=400)

        booking.booking_status = 'InProgress'
        booking.started_at = timezone.now()
        booking.save(update_fields=['booking_status', 'started_at'])

        # Notify counterparty
        counterparty = assignment.accepter_id if is_poster and assignment else booking.poster_id
        actor_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
        cats = ", ".join(booking.service_category) if isinstance(booking.service_category, list) else str(booking.service_category)
        send_in_app_notification(
            receiver=counterparty,
            sender=request.user,
            message=f"{actor_name} marked the {cats} job as In Progress."
        )

        return Response({
            'message': 'Booking is now In Progress.',
            'booking': BookingDetailSerializer(booking, context={'request': request}).data
        }, status=status.HTTP_200_OK)


class BookingCompleteView(APIView):
    """
    Marks a booking as 'Completed' (Tier 1-1).
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, booking_id):
        try:
            booking = tbl_booking.objects.select_for_update().get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return Response({'error': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        assignment = booking.assignments.select_related('accepter_id').first()
        is_accepter = assignment and assignment.accepter_id == request.user
        is_poster = booking.poster_id == request.user

        if not (is_poster or is_accepter):
            return Response({'error': 'You are not authorized to complete this booking.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.booking_status != 'InProgress' or not booking.started_at or booking.started_at > timezone.now() or timezone.now() < booking.start_time:
            return Response({'error': f'Booking cannot be completed from {booking.booking_status} status.'},
                            status=status.HTTP_400_BAD_REQUEST)

        booking.booking_status = 'Completed'
        booking.completed_at = timezone.now()
        booking.save(update_fields=['booking_status', 'completed_at'])

        # Notify counterparty and encourage review
        counterparty = assignment.accepter_id if is_poster and assignment else booking.poster_id
        cats = ", ".join(booking.service_category) if isinstance(booking.service_category, list) else str(booking.service_category)
        send_in_app_notification(
            receiver=counterparty,
            sender=request.user,
            message=f"Job for {cats} is marked as Completed! Please share your feedback and leave a review."
        )

        return Response({
            'message': 'Booking marked as Completed.',
            'booking': BookingDetailSerializer(booking, context={'request': request}).data
        }, status=status.HTTP_200_OK)


class BookingCancelView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [BookingThrottle]

    def get(self, request, booking_id):
        booking = get_object_or_404(tbl_booking, pk=booking_id)
        return Response(cancellation_preview(booking, request.user))

    def patch(self, request, booking_id):
        serializer = BookingCancellationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        booking, actor, changed = cancel_booking(booking_id, request.user,
            serializer.validated_data['reason'], serializer.validated_data.get('expected_points'),
            pending_only=serializer.validated_data['pending_only'])
        return Response({
            'message': 'Booking has been cancelled.',
            'booking': BookingDetailSerializer(booking, context={'request': request}).data,
            'penalty': {**penalty_status(actor), 'points_added': booking.cancellation_penalty_points if changed else 0},
            'safety_advisory': actor.account_type == 'Kasambahay' and booking.cancellation_reason == SAFETY_REASON,
        }, status=status.HTTP_200_OK)


class BookingPenaltyStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(penalty_status(request.user))


class MyBookingsView(generics.ListAPIView):
    """
    List all bookings posted by the authenticated user (Tier 1-4).
    Query param ?status=Active|Completed|Cancelled (where Active = Pending, Accepted, InProgress).
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BookingDetailSerializer

    def get_queryset(self):
        user = self.request.user
        qs = with_post_state(tbl_booking.objects.filter(poster_id=user)).select_related('poster_id').order_by('-createdAt')
        public_only = self.request.query_params.get('public_only')
        if public_only not in (None, 'true', 'false'):
            raise ValidationError({'public_only': 'Use true or false.'})
        if public_only == 'true':
            qs = qs.filter(recipient_id__isnull=True)

        status_param = self.request.query_params.get('status', '').strip().lower()
        if status_param == 'active':
            qs = qs.filter(booking_status__in=['Accepted', 'InProgress'])
        elif status_param == 'pending':
            qs = qs.filter(booking_status='Pending')
        elif status_param == 'completed':
            qs = qs.filter(booking_status='Completed')
        elif status_param == 'cancelled':
            qs = qs.filter(booking_status='Cancelled')
        elif status_param:
            qs = qs.filter(booking_status__iexact=status_param)

        return qs


class MyAssignedBookingsView(generics.ListAPIView):
    """
    List all bookings where the authenticated user is the assigned worker (Tier 1-4).
    Query param ?status=Active|Pending|Completed|Cancelled.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BookingDetailSerializer

    def get_queryset(self):
        user = self.request.user
        qs = tbl_booking.objects.filter(
            Q(assignments__accepter_id=user) | Q(recipient_id=user)
        ).select_related('poster_id').order_by('-createdAt')

        status_param = self.request.query_params.get('status', '').strip().lower()
        if status_param == 'active':
            qs = qs.filter(booking_status__in=['Accepted', 'InProgress'])
        elif status_param == 'pending':
            qs = qs.filter(booking_status='Pending')
        elif status_param == 'completed':
            qs = qs.filter(booking_status='Completed')
        elif status_param == 'cancelled':
            qs = qs.filter(booking_status='Cancelled')
        elif status_param:
            qs = qs.filter(booking_status__iexact=status_param)

        return qs


class BookingProposalCreateView(APIView):
    """
    Submit a counter-offer / proposal for a booking (Tier 2-1).
    """
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def post(self, request, booking_id):
        apply_at_posted_rate = request.data.get('apply_at_posted_rate', False)
        if not isinstance(apply_at_posted_rate, bool):
            raise ValidationError({'apply_at_posted_rate': 'Use true or false.'})
        message = str(request.data.get('message') or '').strip()
        if len(message) > 500:
            raise ValidationError({'message': 'Proposal message cannot exceed 500 characters.'})
        if not apply_at_posted_rate and 'rate_period' not in request.data:
            raise ValidationError({'rate_period': 'Specify day or month for the proposed amount.'})
        proposal, created = create_proposal(booking_id, request.user, request.data.get('proposed_rate'),
                                           request.data.get('rate_period'), message,
                                           apply_at_posted_rate=apply_at_posted_rate)
        if not created:
            return Response({'message': 'Application already submitted.',
                             'proposal': BookingProposalSerializer(proposal).data, 'already_applied': True})
        booking = proposal.booking_id
        from chat.models import tbl_chat_message
        tbl_chat_message.objects.create(sender_id=request.user, receiver_id=booking.poster_id,
            booking_id=booking, booking_proposal=proposal, is_booking_offer=True,
            message_payload='Booking proposal: review the proposed compensation and post terms.')
        if apply_at_posted_rate:
            tbl_chat_message.objects.create(sender_id=request.user, receiver_id=booking.poster_id,
                booking_id=booking, message_payload=proposal.message or 'I am interested in this job position')
        name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
        action = 'applied for your job' if booking.poster_id.account_type == 'Homeowner' else 'sent a hiring offer for your service'
        send_in_app_notification(receiver=booking.poster_id, sender=request.user,
                                 message=f"{name} {action} at ₱{proposal.proposed_rate}/{booking.rate_period}.")
        from chat.moderation import LANGUAGE_NOTICE
        return Response({'message': 'Proposal submitted successfully.',
                         'proposal': BookingProposalSerializer(proposal).data,
                         'language_warning': LANGUAGE_NOTICE if proposal.message != message else None}, status=201)


class MyProposalBookingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = tbl_booking_proposal.objects.filter(proposer_id=request.user).order_by('-createdAt')
        latest = {}
        for proposal in rows:
            latest.setdefault(proposal.booking_id_id, proposal)
        listings = {row.pk: row for row in with_post_state(tbl_booking.objects.filter(pk__in=latest)).select_related('poster_id')}
        bookings = []
        for booking_id, proposal in latest.items():
            listing = listings[booking_id]
            listing.viewer_application = proposal
            bookings.append(listing)
        return Response(ApplicationPostSerializer(bookings, many=True, context={'request': request}).data)


class BookingProposalListView(generics.ListAPIView):
    """
    List all proposals for a booking (Tier 2-1).
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BookingProposalSerializer

    def get_queryset(self):
        booking_id = self.kwargs.get('booking_id')
        user = self.request.user

        try:
            booking = tbl_booking.objects.get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return tbl_booking_proposal.objects.none()

        # If user is poster, they see all proposals; otherwise, only their own
        if booking.poster_id == user:
            return tbl_booking_proposal.objects.filter(booking_id=booking).select_related('proposer_id').order_by('-createdAt')
        else:
            return tbl_booking_proposal.objects.filter(booking_id=booking, proposer_id=user).select_related('proposer_id').order_by('-createdAt')


class BookingProposalRespondView(APIView):
    """
    Poster accepts or rejects a counter-offer proposal (Tier 2-1).
    If accepted:
      - proposal.status = 'Accepted'
      - booking.rate_amount = proposal.proposed_rate
      - booking.booking_status = 'Accepted'
      - creates tbl_booking_assignment with accepter_id = proposal.proposer_id
      - rejects other pending proposals
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, proposal_id):
        try:
            proposal = tbl_booking_proposal.objects.select_related('booking_id', 'proposer_id').get(proposal_id=proposal_id)
        except tbl_booking_proposal.DoesNotExist:
            return Response({'error': 'Proposal not found.'}, status=status.HTTP_404_NOT_FOUND)

        booking = proposal.booking_id
        if booking.poster_id != request.user:
            return Response({'error': 'Only the booking creator can respond to proposals.'}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get('action', '').strip().lower()
        if action not in ['accept', 'reject']:
            return Response({'error': "Action must be either 'accept' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)

        if proposal.status != 'Pending':
            return Response({'error': f'Proposal has already been {proposal.status}.'}, status=status.HTTP_400_BAD_REQUEST)

        if action == 'accept':
            booking, accepter = accept_engagement(booking.pk, request.user, proposal.pk)
            proposal.refresh_from_db()

            # Notify proposer
            poster_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
            send_in_app_notification(
                receiver=proposal.proposer_id,
                sender=request.user,
                message=f"{poster_name} accepted your offer of P{proposal.proposed_rate}! The booking is now confirmed."
            )

            return Response({
                'message': 'Proposal accepted and booking confirmed.',
                'proposal': BookingProposalSerializer(proposal).data,
                'booking': BookingDetailSerializer(booking, context={'request': request}).data
            }, status=status.HTTP_200_OK)
        else:
            if not tbl_booking_proposal.objects.filter(pk=proposal.pk, status='Pending').update(status='Rejected'):
                raise ValidationError({'error': 'This proposal has already been resolved.'})
            proposal.refresh_from_db()
            publish([proposal.proposer_id_id, booking.poster_id_id], {
                'type': 'booking.changed', 'booking_id': str(booking.pk),
            })

            # Notify proposer
            send_in_app_notification(
                receiver=proposal.proposer_id,
                sender=request.user,
                message="Your counter-offer for booking was declined."
            )

            return Response({
                'message': 'Proposal rejected.',
                'proposal': BookingProposalSerializer(proposal).data
            }, status=status.HTTP_200_OK)


class BookingRecommendationsView(APIView):
    """
    Smart Matching / Recommendation Engine (Tier 3-2).
    - If user is Homeowner: Recommends top matching Kasambahay workers based on category match, rating, and location.
    - If user is Kasambahay: Recommends top matching open Job postings based on user's skills/tags and location.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        recommendations = []

        if user.account_type == 'Homeowner':
            # Recommend workers
            workers = User.objects.filter(account_type='Kasambahay', is_active=True,
                pk__in=available_public_posts().values('poster_id')).exclude(id=user.id)
            user_location = (user.city or user.province or '').lower()

            scored_workers = []
            for w in workers:
                score = 0
                # Location matching (+30)
                w_loc = (w.city or w.province or '').lower()
                if user_location and w_loc and (user_location in w_loc or w_loc in user_location):
                    score += 30

                # Rating matching (+0 to 40)
                avg_r = tbl_review.objects.filter(reviewee_id=w).aggregate(avg=Avg('rating'))['avg']
                rating_val = float(avg_r) if avg_r is not None else 0.0
                score += int(rating_val * 8)

                # Verification bonus (+20)
                if getattr(w, 'verification_status', None) == 'Verified':
                    score += 20

                # Has tags (+10)
                if w.user_tags and len(w.user_tags) > 0:
                    score += 10

                from .serializers import get_signed_avatar
                first = w.first_name or ''
                last = w.last_name or ''
                scored_workers.append({
                    'id': str(w.id),
                    'name': f"{first} {last}".strip() or w.username,
                    'role': 'Kasambahay',
                    'match_score': min(score, 100),
                    'rating': round(rating_val, 1),
                    'location': f"{w.city or ''}, {w.province or ''}".strip(', ') or '',
                    'tags': w.user_tags or [],
                    'avatar': get_signed_avatar(w),
                    'verification_status': w.verification_status,
                })

            scored_workers.sort(key=lambda x: x['match_score'], reverse=True)
            recommendations = scored_workers[:15]

        else:
            # Kasambahay: Recommend open Pending jobs from Homeowners
            pending_jobs = available_public_posts(tbl_booking.objects.filter(
                poster_id__account_type='Homeowner'
            )).select_related('poster_id')

            user_tags = [t.lower() for t in (user.user_tags or [])]
            user_loc = (user.city or user.province or '').lower()

            scored_jobs = []
            for job in pending_jobs:
                score = 30  # base
                # Tag / Category overlap
                cats = [c.lower() for c in (job.service_category or [])]
                for cat in cats:
                    if any(t in cat or cat in t for t in user_tags):
                        score += 35
                        break

                # Location match
                job_loc_str = f"{job.barangay or ''} {job.city or ''} {job.street or ''}".lower()
                if user_loc and user_loc in job_loc_str:
                    score += 25

                # Fair rate bonus
                if job.rate_amount >= Decimal('500.00'):
                    score += 10

                from .serializers import get_signed_avatar
                p_first = job.poster_id.first_name or ''
                p_last = job.poster_id.last_name or ''
                display_loc = f"{job.barangay}, {job.city}" if (job.barangay and job.city) else (job.full_address or '')
                scored_jobs.append({
                    'booking_id': str(job.booking_id),
                    'title': ", ".join(job.service_category) if isinstance(job.service_category, list) else str(job.service_category),
                    'rate_amount': str(job.rate_amount),
                    'rate_period': job.rate_period,
                    'location': display_loc,
                    'barangay': job.barangay,
                    'city': job.city,
                    'street': job.street,
                    'province': job.province,
                    'match_score': min(score, 100),
                    'poster_name': f"{p_first} {p_last}".strip() or job.poster_id.username,
                    'poster_avatar': get_signed_avatar(job.poster_id),
                    'booking_type': job.booking_type,
                    'work_setup': job.work_setup,
                    'createdAt': str(job.createdAt),
                })

            scored_jobs.sort(key=lambda x: x['match_score'], reverse=True)
            recommendations = scored_jobs[:15]

        return Response({
            'recommendations': recommendations,
            'count': len(recommendations)
        }, status=status.HTTP_200_OK)


class BookingMinimumWageView(APIView):
    """
    Returns statutory minimum wage rules and guidelines for long-term and short-term bookings
    under Batas Kasambahay (Republic Act No. 10361).
    """
    permission_classes = [AllowAny]

    def get(self, request):
        raw_type = request.query_params.get('booking_type', 'long_term')
        booking_type = str(raw_type or '').strip().lower()
        if booking_type not in ['long_term', 'short_term']:
            return Response(
                {'error': "Invalid booking_type. Must be either 'long_term' or 'short_term'."},
                status=status.HTTP_400_BAD_REQUEST
            )
        address = str(request.query_params.get('address', '') or '').strip()
        info = get_minimum_wage_info(booking_type, address, request.query_params.get('region', ''))
        return Response(info, status=status.HTTP_200_OK)


class BookingSaveToggleView(APIView):
    """
    Toggles saving / bookmarking a booking post for the authenticated user.
    POST /api/v1/booking/save/
    Body: {"booking_id": "<uuid>"}
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        booking_id = request.data.get('booking_id')
        if not booking_id:
            return Response(
                {"detail": "booking_id is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if not check_valid_uuid(str(booking_id)):
            return Response(
                {"detail": "Invalid booking_id format. Must be a valid UUID."},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            booking = tbl_booking.objects.get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return Response(
                {"detail": "Booking post not found."},
                status=status.HTTP_404_NOT_FOUND
            )

        if booking.recipient_id_id and request.user.pk not in (booking.poster_id_id, booking.recipient_id_id):
            raise PermissionDenied('Private offers are only available to their participants.')
        saved_record = tbl_saved_post.objects.filter(user=request.user, booking=booking).first()
        if saved_record:
            saved_record.delete()
            return Response({
                "message": "Post removed from saved",
                "is_saved": False,
                "booking_id": str(booking.booking_id)
            }, status=status.HTTP_200_OK)
        else:
            tbl_saved_post.objects.create(user=request.user, booking=booking)
            return Response({
                "message": "Post saved successfully",
                "is_saved": True,
                "booking_id": str(booking.booking_id)
            }, status=status.HTTP_200_OK)


class SavedBookingListView(APIView):
    """
    Retrieves all saved posts for the authenticated user.
    GET /api/v1/booking/saved/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        saved_qs = tbl_saved_post.objects.filter(
            user=request.user
        ).select_related(
            'booking', 'booking__poster_id'
        ).order_by('-created_at')

        results = []
        for item in saved_qs:
            if item.booking:
                serialized = SavedPostItemSerializer(item).data
                if serialized:
                    results.append(serialized)

        return Response(results, status=status.HTTP_200_OK)



class BookingRateReviewView(APIView):
    permission_classes = [IsAuthenticated]

    @transaction.atomic
    def patch(self, request, booking_id):
        booking = get_object_or_404(tbl_booking.objects.select_for_update(), pk=booking_id)
        if booking.poster_id_id != request.user.pk:
            raise PermissionDenied('Only the poster can confirm the compensation.')
        if booking.booking_status != 'Pending' or booking.assignments.exists():
            raise ValidationError({'error': 'Confirmed engagements cannot be repriced through a post.'})
        if booking.recipient_id_id:
            raise ValidationError({'error': 'Withdraw this private offer and send a new offer to change its agreed terms.'})
        if 'rate_period' not in request.data:
            raise ValidationError({'rate_period': 'Specify day or month.'})
        amount = validate_rate(request.data.get('rate_amount'), booking.booking_type,
                               booking.region, booking.full_address, period=request.data['rate_period'])
        booking.rate_amount = amount
        booking.rate_needs_review = False
        booking.save(update_fields=['rate_amount', 'rate_needs_review'])
        booking.proposals.filter(status='Pending').update(status='Rejected')
        return Response(BookingDetailSerializer(booking, context={'request': request}).data)


class BookingRepostView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [BookingThrottle]

    def post(self, request, booking_id):
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({'detail': 'A valid UUID Idempotency-Key is required to repost.'})
        serializer = RepostScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        listing, created = repost_public_post(booking_id, request.user, serializer.validated_data, key)
        return Response(BookingDetailSerializer(listing, context={'request': request}).data, status=201 if created else 200)


class BookingContractTemplateView(APIView):
    # The blank government form contains no user or booking data.
    permission_classes = [AllowAny]

    def get(self, request):
        from pathlib import Path
        from django.http import FileResponse
        template = Path(__file__).parent / 'assets' / 'dole-standard-kasambahay-contract.pdf'
        return FileResponse(template.open('rb'), content_type='application/pdf', as_attachment=True,
                            filename='DOLE-Standard-Kasambahay-Contract.pdf')
