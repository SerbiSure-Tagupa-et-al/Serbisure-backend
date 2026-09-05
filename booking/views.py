from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle
from rest_framework.exceptions import Throttled
from core.utils import check_valid_uuid
from rest_framework import status
from rest_framework.response import Response
from rest_framework import generics
from .serializers import BookingSerializer, BookingFeedSerializer
from .models import tbl_booking
from django.core.cache import cache
from django.db.models import Q
from core.utils import check_input_letters
from decimal import Decimal, InvalidOperation
import math 

# Create your views here.

class BookingThrottle(UserRateThrottle):
    rate = '50/d'

class BookingView(generics.CreateAPIView):

    throttle_classes = [BookingThrottle]
    serializer_class = BookingSerializer
    queryset = tbl_booking.objects.all()
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):

        if request.user.verification_status != "Verified":
            return Response({
                "detail": "Only verified user can post"},
                status=status.HTTP_403_FORBIDDEN
            )

        idempotency_key = request.headers.get('Idempotency-Key')

        if not idempotency_key or not check_valid_uuid(idempotency_key):
            return Response({"detail": "The Idempotency-Key header is required and must be a valid UUID v4."},
            status=status.HTTP_400_BAD_REQUEST)

        cached_response = cache.get(idempotency_key)

        if cached_response:
            return Response(cached_response['data'], status=cached_response['status'])
        
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid(raise_exception=True):

            serializer.save(poster_id=request.user)

            response_data = {
                "message" : "Booking posted successfully",
                "data": serializer.data
            }
            response_status = status.HTTP_201_CREATED

            if idempotency_key:
                cache.set(
                    idempotency_key,
                    {'data': response_data, 'status': response_status},
                    timeout=86400 # 86400 seconds = 24 hours
                )

            return Response(
                response_data,
                status=response_status
            )

        return super().create(request, *args, **kwargs)
    
    def throttled(self, request, wait):
        # 3600 seconds = 1 hour
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

        # Identify the user who is making the request
        current_user = self.request.user

        # Get the base query (only show 'Pending' bookings in the feed, ignoring completed ones)
        queryset = tbl_booking.objects.select_related('poster_id').filter(booking_status='Pending')

        # Filter based on account type
        if current_user.account_type == 'Homeowner':
            # Homeowners only see posts made by Kasambahays
            queryset = queryset.filter(poster_id__account_type='Kasambahay')
        elif current_user.account_type == 'Kasambahay':
            # Kasambahays only see posts made by Homeowners
            queryset = queryset.filter(poster_id__account_type='Homeowner')

        params = self.request.query_params

        # 1. Service Category filter (supports comma-separated list e.g. ?category=Cleaning,Cooking)
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
                try:
                    queryset = queryset.filter(service_category__overlap=mapped_cats)
                except Exception:
                    cat_q = Q()
                    for cat in mapped_cats:
                        cat_q |= Q(service_category__icontains=cat)
                    queryset = queryset.filter(cat_q)

        # 2. Booking type filter (?booking_type=short_term | long_term)
        bt_param = params.get('booking_type')
        if bt_param:
            bt_norm = bt_param.lower().replace('-', '_').strip()
            if bt_norm in ['short_term', 'part_time', 'parttime']:
                queryset = queryset.filter(booking_type='short_term')
            elif bt_norm in ['long_term', 'stay_in', 'stayin']:
                queryset = queryset.filter(booking_type='long_term')

        # 3. Max & Min daily rate (?max_rate=1500&min_rate=500)
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

        # 4. Location filter (?location=Cagayan)
        location = params.get('location')
        if location and location.strip():
            queryset = queryset.filter(service_address__icontains=location.strip())

        # 5. Search keyword (?search= or ?q=)
        search_kw = params.get('search') or params.get('q')
        if search_kw and search_kw.strip():
            kw = search_kw.strip()
            queryset = queryset.filter(
                Q(service_address__icontains=kw) |
                Q(special_instruction__icontains=kw) |
                Q(poster_id__first_name__icontains=kw) |
                Q(poster_id__last_name__icontains=kw)
            )

        # 6. Sorting (?sort=rate_asc | rate_desc | oldest | newest)
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