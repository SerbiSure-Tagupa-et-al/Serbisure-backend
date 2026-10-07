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
    BookingDetailSerializer,
    BookingProposalSerializer
)
from .models import tbl_booking, tbl_booking_assignment, tbl_booking_proposal
from reviews.models import tbl_review
from django.core.cache import cache
from django.db.models import Q, Avg
from decimal import Decimal, InvalidOperation
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from chat.models import tbl_chat_message
from notifications.services import send_in_app_notification
from .wage_policy import get_minimum_daily_wage, get_monthly_equivalent, get_minimum_wage_info
import math 

User = get_user_model()


class BookingThrottle(UserRateThrottle):
    rate = '50/d'


class BookingView(generics.CreateAPIView):
    throttle_classes = [BookingThrottle]
    serializer_class = BookingSerializer
    queryset = tbl_booking.objects.all()
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        if getattr(request.user, 'is_restricted', False):
            return Response({
                "code": "account_restricted",
                "detail": "Your account has been restricted from creating or accepting bookings due to 3 cancellation strikes. Please contact support."
            }, status=status.HTTP_403_FORBIDDEN)

        if request.user.verification_status != "Verified":
            return Response({
                "code": "account_not_verified",
                "detail": "Account verification is required before posting. Please verify your account to keep our community safe and trusted.",
                "verification_status": request.user.verification_status
            }, status=status.HTTP_403_FORBIDDEN)

        idempotency_key = request.headers.get('Idempotency-Key')
        if not idempotency_key or not check_valid_uuid(idempotency_key):
            return Response({"detail": "The Idempotency-Key header is required and must be a valid UUID v4."},
                            status=status.HTTP_400_BAD_REQUEST)

        cached_response = cache.get(idempotency_key)
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
                    idempotency_key,
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
        queryset = tbl_booking.objects.select_related('poster_id').filter(booking_status='Pending')

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
            elif bt_norm in ['long_term', 'stay_in', 'stayin']:
                queryset = queryset.filter(booking_type='long_term')

        # 3. Max & Min daily rate
        max_rate = params.get('max_rate')
        if max_rate:
            try:
                queryset = queryset.filter(daily_rate__lte=Decimal(str(max_rate)))
            except (InvalidOperation, ValueError, TypeError):
                pass

        min_rate = params.get('min_rate')
        if min_rate:
            try:
                queryset = queryset.filter(daily_rate__gte=Decimal(str(min_rate)))
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
        sort = params.get('sort', 'newest')
        if sort == 'rate_asc':
            queryset = queryset.order_by('daily_rate')
        elif sort == 'rate_desc':
            queryset = queryset.order_by('-daily_rate')
        elif sort == 'oldest':
            queryset = queryset.order_by('createdAt')
        else:
            queryset = queryset.order_by('-createdAt')

        return queryset


class BookingDetailView(generics.RetrieveAPIView):
    """
    Retrieve full details of a specific booking by UUID (Tier 1-1).
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BookingDetailSerializer
    queryset = tbl_booking.objects.select_related('poster_id').all()
    lookup_field = 'booking_id'


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
        try:
            booking = tbl_booking.objects.get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return Response({'error': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        if booking.poster_id == request.user:
            return Response({'error': 'You cannot accept your own booking.'}, status=status.HTTP_400_BAD_REQUEST)

        if booking.booking_status != 'Pending':
            return Response({'error': 'This job position has already been acquired.', 'code': 'already_acquired'},
                            status=status.HTTP_400_BAD_REQUEST)

        existing_assignment = tbl_booking_assignment.objects.filter(booking_id=booking).first()
        if existing_assignment and existing_assignment.accepter_id and existing_assignment.accepter_id != request.user:
            return Response({'error': 'This job position has already been acquired.', 'code': 'already_acquired'},
                            status=status.HTTP_400_BAD_REQUEST)

        booking.booking_status = 'Accepted'
        booking.save(update_fields=['booking_status'])

        assignment, _ = tbl_booking_assignment.objects.get_or_create(
            booking_id=booking,
            defaults={'accepter_id': request.user}
        )
        if assignment.accepter_id != request.user:
            assignment.accepter_id = request.user
            assignment.save(update_fields=['accepter_id'])

        # Send in-app notification to the poster
        accepter_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
        cats = ", ".join(booking.service_category) if isinstance(booking.service_category, list) else str(booking.service_category)
        send_in_app_notification(
            receiver=booking.poster_id,
            sender=request.user,
            message=f"{accepter_name} has accepted your booking request for {cats}!"
        )

        # Notify other applicants who messaged/proposed about this post (Marked as Closed / FB Marketplace style)
        notify_other_applicants_listing_closed(booking, request.user)

        return Response({
            'message': 'Booking accepted successfully.',
            'booking': BookingDetailSerializer(booking, context={'request': request}).data
        }, status=status.HTTP_200_OK)


def notify_other_applicants_listing_closed(booking, accepted_user):
    """
    When an applicant is accepted and contract is formalized,
    notify all other applicants who messaged the homeowner or sent proposals that the
    job position is already acquired (in simple English).
    """
    try:
        from chat.models import tbl_chat_message
        cats = ", ".join(booking.service_category) if isinstance(booking.service_category, list) else str(booking.service_category)
        other_applicants = {}

        # Resolve homeowner and accepted kasambahay
        if getattr(booking.poster_id, 'account_type', None) == 'Homeowner':
            homeowner = booking.poster_id
            accepted_kasambahay = accepted_user
        elif getattr(accepted_user, 'account_type', None) == 'Homeowner':
            homeowner = accepted_user
            accepted_kasambahay = booking.poster_id
        else:
            homeowner = booking.poster_id
            accepted_kasambahay = accepted_user

        accepted_user_id = getattr(accepted_kasambahay, 'id', accepted_kasambahay)
        homeowner_id = getattr(homeowner, 'id', homeowner)

        # 1. Applicants who submitted counter-proposals
        for prop in tbl_booking_proposal.objects.filter(booking_id=booking).exclude(proposer_id__in=[accepted_user_id, homeowner_id]):
            if prop.proposer_id:
                other_applicants[prop.proposer_id.id] = prop.proposer_id

        # 2. Applicants who sent or received chat messages linked to this booking
        for msg in tbl_chat_message.objects.filter(booking_id=booking).exclude(sender_id__in=[accepted_user_id, homeowner_id]):
            if msg.sender_id and getattr(msg.sender_id, 'account_type', None) == 'Kasambahay':
                other_applicants[msg.sender_id.id] = msg.sender_id

        for msg in tbl_chat_message.objects.filter(booking_id=booking).exclude(receiver_id__in=[accepted_user_id, homeowner_id]):
            if msg.receiver_id and getattr(msg.receiver_id, 'account_type', None) == 'Kasambahay':
                other_applicants[msg.receiver_id.id] = msg.receiver_id

        # 3. All Kasambahays who sent messages to this homeowner (e.g. applications/inquiries)
        kasambahay_senders = tbl_chat_message.objects.filter(
            receiver_id=homeowner,
            sender_id__account_type='Kasambahay'
        ).exclude(sender_id__in=[accepted_user_id, homeowner_id]).values_list('sender_id', flat=True).distinct()

        for uid in kasambahay_senders:
            try:
                applicant_user = User.objects.get(pk=uid)
                other_applicants[applicant_user.id] = applicant_user
            except User.DoesNotExist:
                pass

        kasambahay_receivers = tbl_chat_message.objects.filter(
            sender_id=homeowner,
            receiver_id__account_type='Kasambahay'
        ).exclude(receiver_id__in=[accepted_user_id, homeowner_id]).values_list('receiver_id', flat=True).distinct()

        for uid in kasambahay_receivers:
            try:
                applicant_user = User.objects.get(pk=uid)
                other_applicants[applicant_user.id] = applicant_user
            except User.DoesNotExist:
                pass

        # Reject any other pending proposals for this booking
        tbl_booking_proposal.objects.filter(
            booking_id=booking,
            status='Pending'
        ).exclude(proposer_id__in=[accepted_user_id, homeowner_id]).update(status='Rejected')

        closed_notification_msg = f"This job position ({cats}) has already been acquired. Thank you for your application!"
        closed_chat_msg = f"[JOB_ACQUIRED]: This job position has already been acquired. Thank you for your interest!"

        sent_count = 0
        for uid, applicant in other_applicants.items():
            # Check if job acquired chat was already sent recently (Python check since message_payload is encrypted with Fernet)
            recent_msgs = tbl_chat_message.objects.filter(
                sender_id=homeowner,
                receiver_id=applicant
            ).order_by('-createdAt')[:10]

            already_sent = any(
                m.message_payload and ('[JOB_ACQUIRED]' in m.message_payload or 'already been acquired' in m.message_payload.lower())
                for m in recent_msgs
            )

            if not already_sent:
                send_in_app_notification(
                    receiver=applicant,
                    sender=homeowner,
                    message=closed_notification_msg
                )
                try:
                    tbl_chat_message.objects.create(
                        sender_id=homeowner,
                        receiver_id=applicant,
                        booking_id=booking,
                        message_type='text',
                        message_payload=closed_chat_msg
                    )
                    sent_count += 1
                except Exception as msg_err:
                    import logging
                    logging.getLogger(__name__).error(f"[notify_other_applicants_listing_closed] Chat create error: {msg_err}")

        return sent_count
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"[notify_other_applicants_listing_closed] Error: {e}")
        return 0


class BookingConfirmContractView(APIView):
    """
    Called when a booking contract is formalized and confirmed (Tier 1-1).
    - Prevents double contracts if the position is already acquired.
    - Sets booking_status to 'Accepted' so it disappears from open feeds.
    - Creates or updates tbl_booking_assignment.
    - Notifies all other Kasambahays that the job position is already acquired.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if getattr(request.user, 'is_restricted', False):
            return Response({
                'error': 'Your account is restricted from confirming contracts due to 3 cancellation strikes.',
                'code': 'account_restricted'
            }, status=status.HTTP_403_FORBIDDEN)

        booking_id = request.data.get('booking_id')
        partner_id = request.data.get('partner_id')

        # Resolve accepter
        accepter = None
        partner = None
        if partner_id:
            try:
                partner = User.objects.get(pk=partner_id)
            except (User.DoesNotExist, Exception):
                pass

        booking = None
        if booking_id:
            try:
                booking = tbl_booking.objects.get(booking_id=booking_id)
            except (tbl_booking.DoesNotExist, Exception):
                pass

        if not booking and partner:
            # Look for pending booking posted by partner or request.user
            booking = tbl_booking.objects.filter(
                Q(poster_id=partner) | Q(poster_id=request.user),
                booking_status='Pending'
            ).order_by('-createdAt').first()

            if not booking:
                # Check if there is already an accepted booking
                already_accepted = tbl_booking.objects.filter(
                    Q(poster_id=partner) | Q(poster_id=request.user),
                    booking_status='Accepted'
                ).order_by('-createdAt').first()

                if already_accepted:
                    existing_assign = tbl_booking_assignment.objects.filter(booking_id=already_accepted).first()
                    # Check if someone else already acquired this job position
                    target_accepter_id = request.user.id if request.user.account_type == 'Kasambahay' else (partner.id if partner else None)
                    if existing_assign and target_accepter_id and existing_assign.accepter_id_id != target_accepter_id:
                        return Response({
                            'error': 'This job position has already been acquired.',
                            'code': 'already_acquired'
                        }, status=status.HTTP_400_BAD_REQUEST)
                    booking = already_accepted

        # Resolve accepter user
        if booking and booking.poster_id == request.user and partner:
            accepter = partner
        elif booking and booking.poster_id != request.user:
            accepter = request.user
        elif partner and partner.account_type == 'Kasambahay':
            accepter = partner
        else:
            accepter = request.user

        # Guard: Check if the booking already has an active contract with ANOTHER kasambahay (prevent double contract)
        if booking:
            existing_assignment = tbl_booking_assignment.objects.filter(booking_id=booking).first()
            if existing_assignment and existing_assignment.accepter_id:
                if accepter and existing_assignment.accepter_id != accepter:
                    return Response({
                        'error': 'This job position has already been acquired.',
                        'code': 'already_acquired'
                    }, status=status.HTTP_400_BAD_REQUEST)

        if not booking:
            poster = request.user
            if partner and partner.account_type == 'Homeowner':
                poster = partner

            from django.utils import timezone
            booking = tbl_booking.objects.create(
                poster_id=poster,
                booking_type='long_term',
                service_category=['All-around'],
                start_time=timezone.now(),
                daily_rate=Decimal('6500'),
                booking_status='Accepted',
                special_instruction='Contract formalized via chat'
            )

        booking.booking_status = 'Accepted'
        booking.save(update_fields=['booking_status'])

        if accepter:
            tbl_booking_assignment.objects.update_or_create(
                booking_id=booking,
                defaults={'accepter_id': accepter}
            )

        notified_count = notify_other_applicants_listing_closed(booking, accepter or request.user)

        # Notify the poster about confirmation
        cats = ", ".join(booking.service_category) if isinstance(booking.service_category, list) else str(booking.service_category)
        accepter_name = f"{accepter.first_name} {accepter.last_name}".strip() or accepter.username if accepter else "Kasambahay"
        if booking.poster_id != request.user:
            send_in_app_notification(
                receiver=booking.poster_id,
                sender=request.user,
                message=f"Contract confirmed! {accepter_name} has accepted your booking request for {cats}!"
            )

        return Response({
            'success': True,
            'message': 'Booking contract confirmed. All other applicants have been notified and listing is closed.',
            'booking_id': str(booking.booking_id),
            'booking_status': booking.booking_status,
            'notified_count': notified_count,
        }, status=status.HTTP_200_OK)



class BookingStartView(APIView):
    """
    Marks an 'Accepted' booking as 'InProgress' (Tier 1-1).
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, booking_id):
        try:
            booking = tbl_booking.objects.get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return Response({'error': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        assignment = booking.assignments.select_related('accepter_id').first()
        is_accepter = assignment and assignment.accepter_id == request.user
        is_poster = booking.poster_id == request.user

        if not (is_poster or is_accepter):
            return Response({'error': 'You are not authorized to update this booking.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.booking_status != 'Accepted':
            return Response({'error': f'Booking must be in Accepted status to start. Current: {booking.booking_status}.'},
                            status=status.HTTP_400_BAD_REQUEST)

        booking.booking_status = 'InProgress'
        booking.save(update_fields=['booking_status'])

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

    def patch(self, request, booking_id):
        try:
            booking = tbl_booking.objects.get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return Response({'error': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        assignment = booking.assignments.select_related('accepter_id').first()
        is_accepter = assignment and assignment.accepter_id == request.user
        is_poster = booking.poster_id == request.user

        if not (is_poster or is_accepter):
            return Response({'error': 'You are not authorized to complete this booking.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.booking_status not in ['InProgress', 'Accepted']:
            return Response({'error': f'Booking cannot be completed from {booking.booking_status} status.'},
                            status=status.HTTP_400_BAD_REQUEST)

        booking.booking_status = 'Completed'
        booking.save(update_fields=['booking_status'])

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
    """
    Cancels a booking under the fair 2-hour mutual cancellation policy:
    1. Unassigned 'Pending' bookings can be cancelled immediately by poster with 0 strikes.
    2. Confirmed bookings ('Accepted' / 'InProgress'):
       - Cancellations are only permitted within 2 hours of confirmation (accepted_at).
       - Both parties must prompt/confirm cancellation:
         a. Initiator calls cancel -> sets cancel_requested_by, notifies counterparty.
         b. Counterparty can confirm -> booking marked 'Cancelled', 1 strike added to initiator.
            If initiator reaches 3 strikes, their account is restricted (is_restricted = True).
         c. Counterparty can decline -> cancel request dismissed, booking remains active.
         d. Initiator can withdraw request -> cancel request dismissed.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, booking_id):
        if getattr(request.user, 'is_restricted', False):
            return Response({
                'error': 'Your account is restricted from managing bookings due to 3 cancellation strikes.'
            }, status=status.HTTP_403_FORBIDDEN)

        try:
            booking = tbl_booking.objects.get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return Response({'error': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        assignment = booking.assignments.select_related('accepter_id').first()
        is_accepter = bool(assignment and assignment.accepter_id == request.user)
        is_poster = (booking.poster_id == request.user)

        if not (is_poster or is_accepter):
            return Response({'error': 'You are not authorized to cancel this booking.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.booking_status in ['Completed', 'Cancelled']:
            return Response({'error': f'Booking is already {booking.booking_status}.'}, status=status.HTTP_400_BAD_REQUEST)

        action = str(request.data.get('action', 'request') or 'request').strip().lower()
        reason = str(request.data.get('reason', '') or '').strip()

        # 1. Unconfirmed / Pending bookings: poster can cancel immediately with 0 strikes
        if booking.booking_status == 'Pending':
            booking.booking_status = 'Cancelled'
            booking.cancelled_by = request.user
            booking.cancel_requested_by = None
            booking.cancellation_reason = reason or 'Cancelled before acceptance'
            booking.save(update_fields=['booking_status', 'cancelled_by', 'cancel_requested_by', 'cancellation_reason'])

            return Response({
                'message': 'Booking has been cancelled.',
                'booking': BookingDetailSerializer(booking, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        # 2. Confirmed bookings ('Accepted' / 'InProgress'):
        # Check 2-hour window policy
        confirmed_at = assignment.accepted_at if (assignment and assignment.accepted_at) else booking.createdAt
        if confirmed_at:
            cancellation_deadline = confirmed_at + timedelta(hours=2)
            if timezone.now() > cancellation_deadline:
                return Response({
                    'error': 'Cancellations are only permitted within 2 hours of booking confirmation. The 2-hour window has expired, so this booking cannot be cancelled.'
                }, status=status.HTTP_400_BAD_REQUEST)

        counterparty = assignment.accepter_id if is_poster else booking.poster_id
        actor_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
        cats = ", ".join(booking.service_category) if isinstance(booking.service_category, list) else str(booking.service_category)

        # A. Decline or Withdraw existing cancel request
        if action == 'decline':
            if not booking.cancel_requested_by:
                return Response({'error': 'There is no active cancellation request for this booking.'}, status=status.HTTP_400_BAD_REQUEST)

            if booking.cancel_requested_by == request.user:
                # Initiator withdraws their own cancellation request
                booking.cancel_requested_by = None
                booking.cancel_requested_at = None
                booking.cancellation_reason = None
                booking.save(update_fields=['cancel_requested_by', 'cancel_requested_at', 'cancellation_reason'])
                return Response({
                    'message': 'You have withdrawn your cancellation request. The booking remains active.',
                    'booking': BookingDetailSerializer(booking, context={'request': request}).data
                }, status=status.HTTP_200_OK)
            else:
                # Counterparty declines the cancellation request
                initiator = booking.cancel_requested_by
                booking.cancel_requested_by = None
                booking.cancel_requested_at = None
                booking.cancellation_reason = None
                booking.save(update_fields=['cancel_requested_by', 'cancel_requested_at', 'cancellation_reason'])

                send_in_app_notification(
                    receiver=initiator,
                    sender=request.user,
                    message=f"{actor_name} declined your cancellation request for {cats}. The booking remains active."
                )
                try:
                    tbl_chat_message.objects.create(
                        sender_id=request.user,
                        receiver_id=initiator,
                        booking_id=booking,
                        message_type='text',
                        message_payload=f"[CANCELLATION_DECLINED]: {actor_name} declined the cancellation request. This booking remains active."
                    )
                except Exception:
                    pass

                return Response({
                    'message': 'Cancellation request declined. The booking remains active.',
                    'booking': BookingDetailSerializer(booking, context={'request': request}).data
                }, status=status.HTTP_200_OK)

        # B. First party requests cancellation
        if booking.cancel_requested_by is None:
            booking.cancel_requested_by = request.user
            booking.cancel_requested_at = timezone.now()
            booking.cancellation_reason = reason or "Cancellation requested within 2-hour window"
            booking.save(update_fields=['cancel_requested_by', 'cancel_requested_at', 'cancellation_reason'])

            if counterparty:
                send_in_app_notification(
                    receiver=counterparty,
                    sender=request.user,
                    message=f"{actor_name} requested to cancel the booking for {cats}. Both parties must confirm cancellation within the 2-hour window."
                )
                try:
                    tbl_chat_message.objects.create(
                        sender_id=request.user,
                        receiver_id=counterparty,
                        booking_id=booking,
                        message_type='text',
                        message_payload=f"[CANCELLATION_REQUESTED]: {actor_name} requested to cancel this booking. Please confirm or decline within the 2-hour cancellation window."
                    )
                except Exception:
                    pass

            return Response({
                'message': 'Cancellation requested. Awaiting confirmation from the other party.',
                'booking': BookingDetailSerializer(booking, context={'request': request}).data
            }, status=status.HTTP_200_OK)

        # C. User already requested cancellation and is attempting again
        if booking.cancel_requested_by == request.user:
            return Response({
                'error': 'You have already requested to cancel this booking. Waiting for the other party to confirm or decline.'
            }, status=status.HTTP_400_BAD_REQUEST)

        # D. Counterparty confirms cancellation -> BOTH parties have now confirmed!
        initiator = booking.cancel_requested_by
        booking.booking_status = 'Cancelled'
        booking.cancelled_by = initiator
        booking.save(update_fields=['booking_status', 'cancelled_by'])

        # Increment cancellation strikes for the initiator (3 strikes = restricted)
        initiator.cancellation_strikes = (getattr(initiator, 'cancellation_strikes', 0) or 0) + 1
        if initiator.cancellation_strikes >= 3:
            initiator.is_restricted = True
            initiator.save(update_fields=['cancellation_strikes', 'is_restricted'])
            send_in_app_notification(
                receiver=initiator,
                sender=None,
                message="Your account has been restricted because you reached 3 booking cancellations. Please contact support."
            )
        else:
            initiator.save(update_fields=['cancellation_strikes'])
            send_in_app_notification(
                receiver=initiator,
                sender=None,
                message=f"Booking cancelled. You received 1 cancellation strike ({initiator.cancellation_strikes}/3). 3 strikes will lead to account restriction."
            )

        # Notify counterparty who just approved
        send_in_app_notification(
            receiver=request.user,
            sender=None,
            message=f"Booking for {cats} cancellation has been mutually confirmed."
        )

        try:
            tbl_chat_message.objects.create(
                sender_id=request.user,
                receiver_id=initiator,
                booking_id=booking,
                message_type='text',
                message_payload=f"[CANCELLATION_CONFIRMED]: Both parties agreed. This booking is cancelled."
            )
        except Exception:
            pass

        return Response({
            'message': 'Booking cancellation mutually confirmed.',
            'booking': BookingDetailSerializer(booking, context={'request': request}).data
        }, status=status.HTTP_200_OK)


class MyBookingsView(generics.ListAPIView):
    """
    List all bookings posted by the authenticated user (Tier 1-4).
    Query param ?status=Active|Completed|Cancelled (where Active = Pending, Accepted, InProgress).
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BookingDetailSerializer

    def get_queryset(self):
        user = self.request.user
        qs = tbl_booking.objects.filter(poster_id=user).select_related('poster_id').order_by('-createdAt')

        status_param = self.request.query_params.get('status', '').strip().lower()
        if status_param == 'active':
            qs = qs.filter(booking_status__in=['Pending', 'Accepted', 'InProgress'])
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
    Query param ?status=Active|Completed|Cancelled.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = BookingDetailSerializer

    def get_queryset(self):
        user = self.request.user
        qs = tbl_booking.objects.filter(
            assignments__accepter_id=user
        ).select_related('poster_id').order_by('-createdAt')

        status_param = self.request.query_params.get('status', '').strip().lower()
        if status_param == 'active':
            qs = qs.filter(booking_status__in=['Pending', 'Accepted', 'InProgress'])
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

    def post(self, request, booking_id):
        try:
            booking = tbl_booking.objects.get(booking_id=booking_id)
        except tbl_booking.DoesNotExist:
            return Response({'error': 'Booking not found.'}, status=status.HTTP_404_NOT_FOUND)

        if getattr(request.user, 'is_restricted', False):
            return Response({'error': 'Your account is restricted from submitting proposals due to 3 cancellation strikes.'}, status=status.HTTP_403_FORBIDDEN)

        if getattr(request.user, 'verification_status', None) != 'Verified':
            return Response({'error': 'Only verified users can submit counter-offer proposals.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.poster_id == request.user:
            return Response({'error': 'You cannot make a proposal on your own booking.'}, status=status.HTTP_400_BAD_REQUEST)

        if getattr(booking.poster_id, 'account_type', None) == request.user.account_type:
            return Response({'error': f'You cannot make a proposal on a booking created by another {request.user.account_type}.'}, status=status.HTTP_400_BAD_REQUEST)

        if booking.booking_status != 'Pending':
            return Response({'error': 'Proposals can only be submitted for Pending bookings.'}, status=status.HTTP_400_BAD_REQUEST)

        # Anti-spam: check if user already has a pending proposal for this booking
        if tbl_booking_proposal.objects.filter(booking_id=booking, proposer_id=request.user, status='Pending').exists():
            return Response({'error': 'You already have an active pending proposal for this booking. Please wait for the other party to respond.'}, status=status.HTTP_400_BAD_REQUEST)

        proposed_rate = request.data.get('proposed_rate')
        message = str(request.data.get('message', '') or '').strip()

        if len(message) > 500:
            return Response({'error': 'Proposal message cannot exceed 500 characters.'}, status=status.HTTP_400_BAD_REQUEST)

        if not proposed_rate:
            return Response({'error': 'Proposed rate is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            rate_val = Decimal(str(proposed_rate))
            if not rate_val.is_finite() or rate_val < Decimal('1.00') or rate_val > Decimal('999999.99'):
                raise ValueError
            rate_val = rate_val.quantize(Decimal('0.01'))
        except Exception:
            return Response({'error': 'Proposed rate must be a valid amount between ₱1.00 and ₱999,999.99.'}, status=status.HTTP_400_BAD_REQUEST)

        # Enforce statutory minimum wage if booking is long_term (Batas Kasambahay RA 10361)
        if booking.booking_type == 'long_term':
            full_address = f"{booking.full_address} {booking.zip_code or ''}".strip()
            min_wage = get_minimum_daily_wage('long_term', full_address)
            if rate_val < min_wage:
                approx_monthly = get_monthly_equivalent(min_wage)
                return Response({
                    'error': (
                        f"Proposed rate for long-term domestic service cannot be below the legal minimum wage "
                        f"of ₱{min_wage:.2f}/day (approx. ₱{approx_monthly:,.2f}/month under Batas Kasambahay RA 10361)."
                    )
                }, status=status.HTTP_400_BAD_REQUEST)

        proposal = tbl_booking_proposal.objects.create(
            booking_id=booking,
            proposer_id=request.user,
            proposed_rate=rate_val,
            message=message
        )

        # Notify poster
        proposer_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
        send_in_app_notification(
            receiver=booking.poster_id,
            sender=request.user,
            message=f"{proposer_name} offered a counter-rate of P{rate_val} for your booking."
        )

        return Response({
            'message': 'Proposal submitted successfully.',
            'proposal': BookingProposalSerializer(proposal).data
        }, status=status.HTTP_201_CREATED)


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
      - booking.daily_rate = proposal.proposed_rate
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
        if getattr(request.user, 'is_restricted', False):
            return Response({'error': 'Your account is restricted from accepting proposals due to 3 cancellation strikes.'}, status=status.HTTP_403_FORBIDDEN)

        if booking.poster_id != request.user:
            return Response({'error': 'Only the booking creator can respond to proposals.'}, status=status.HTTP_403_FORBIDDEN)

        action = request.data.get('action', '').strip().lower()
        if action not in ['accept', 'reject']:
            return Response({'error': "Action must be either 'accept' or 'reject'."}, status=status.HTTP_400_BAD_REQUEST)

        if proposal.status != 'Pending':
            return Response({'error': f'Proposal has already been {proposal.status}.'}, status=status.HTTP_400_BAD_REQUEST)

        if action == 'accept':
            proposal.status = 'Accepted'
            proposal.save(update_fields=['status'])

            booking.daily_rate = proposal.proposed_rate
            booking.booking_status = 'Accepted'
            booking.save(update_fields=['daily_rate', 'booking_status'])

            tbl_booking_assignment.objects.update_or_create(
                booking_id=booking,
                defaults={'accepter_id': proposal.proposer_id}
            )

            # Reject all other pending proposals for this booking
            tbl_booking_proposal.objects.filter(booking_id=booking, status='Pending').exclude(proposal_id=proposal.proposal_id).update(status='Rejected')

            # Notify proposer
            poster_name = f"{request.user.first_name} {request.user.last_name}".strip() or request.user.username
            send_in_app_notification(
                receiver=proposal.proposer_id,
                sender=request.user,
                message=f"{poster_name} accepted your offer of P{proposal.proposed_rate}! The booking is now confirmed."
            )

            # Notify other applicants (FB Marketplace style closure)
            notify_other_applicants_listing_closed(booking, proposal.proposer_id)

            return Response({
                'message': 'Proposal accepted and booking confirmed.',
                'proposal': BookingProposalSerializer(proposal).data,
                'booking': BookingDetailSerializer(booking, context={'request': request}).data
            }, status=status.HTTP_200_OK)
        else:
            proposal.status = 'Rejected'
            proposal.save(update_fields=['status'])

            # Notify proposer
            send_in_app_notification(
                receiver=proposal.proposer_id,
                sender=request.user,
                message=f"Your counter-offer for booking was declined."
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
            workers = User.objects.filter(account_type='Kasambahay', is_active=True).exclude(id=user.id)
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
                rating_val = float(avg_r) if avg_r is not None else 4.0
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
                    'location': f"{w.city or ''}, {w.province or ''}".strip(', ') or 'Philippines',
                    'tags': w.user_tags or [],
                    'avatar': get_signed_avatar(w),
                    'verification_status': w.verification_status,
                })

            scored_workers.sort(key=lambda x: x['match_score'], reverse=True)
            recommendations = scored_workers[:15]

        else:
            # Kasambahay: Recommend open Pending jobs from Homeowners
            pending_jobs = tbl_booking.objects.filter(
                booking_status='Pending',
                poster_id__account_type='Homeowner'
            ).select_related('poster_id')

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
                if job.daily_rate >= Decimal('500.00'):
                    score += 10

                from .serializers import get_signed_avatar
                p_first = job.poster_id.first_name or ''
                p_last = job.poster_id.last_name or ''
                display_loc = f"{job.barangay}, {job.city}" if (job.barangay and job.city) else (job.full_address or 'Cagayan de Oro')
                scored_jobs.append({
                    'booking_id': str(job.booking_id),
                    'title': ", ".join(job.service_category) if isinstance(job.service_category, list) else str(job.service_category),
                    'daily_rate': str(job.daily_rate),
                    'location': display_loc,
                    'barangay': job.barangay,
                    'city': job.city,
                    'street': job.street,
                    'province': job.province,
                    'match_score': min(score, 100),
                    'poster_name': f"{p_first} {p_last}".strip() or job.poster_id.username,
                    'poster_avatar': get_signed_avatar(job.poster_id),
                    'booking_type': job.booking_type,
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
        info = get_minimum_wage_info(booking_type, address)
        return Response(info, status=status.HTTP_200_OK)