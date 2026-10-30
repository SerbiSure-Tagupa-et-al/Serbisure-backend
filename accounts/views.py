from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from .models import tbl_user_profile
from .serializers import (
    UserRegistrationSerializer,
    CustomLoginSerializer,
    UserAboutSerializer,
    UserTagsSerializer,
    ContactPrivacySerializer,
    UserSocialLinksSerializer,
    JobStatusSerializer,
    PublicProfileSerializer,
    KasambahayResumeSerializer,
)
from .permissions import IsKasambahay, IsPortalAdmin, IsPortalSuperadmin
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.exceptions import Throttled
from django.core.cache import cache
from django.db.models import Q
from core.utils import check_valid_uuid, get_signed_cloudinary_url, normalize_ph_phone_number
import math
import re
from django.db import IntegrityError
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

class RegistrationThrottle(AnonRateThrottle):
    # Separate from login's anonymous counter and the previous daily limit.
    scope = 'registration_5min'
    rate = 'custom'

    def parse_rate(self, rate):
        return (5, 300)

class UserRegistrationView(APIView):

    throttle_classes = [RegistrationThrottle]

    # This tell Django: "You do not need to logged in to access this windows."
    # (Because if you had to be logged in to register...nobody could ever register!)

    authentication_classes = []
    permission_classes = []

    @extend_schema(
        request=UserRegistrationSerializer,
        parameters=[
            OpenApiParameter(
                name='Idempotency-Key',
                type=OpenApiTypes.UUID,
                location=OpenApiParameter.HEADER,
                description='A unique UUID v4 string to prevent duplicate registrations',
                required=True,
            )
        ]
    )

    def post(self, request):

        # Look for the special header send by the frontend (or Postman)
        idempotency_key = request.headers.get('Idempotency-Key')

        # If they sent a key, check if we already saved an answer for it
        if not idempotency_key or not check_valid_uuid(idempotency_key):

            return Response({"details": "The Idempotency-Key header is required and must be a valid UUID v4."}, 
                status=status.HTTP_400_BAD_REQUEST)
        
        cached_response = cache.get(idempotency_key)

        if cached_response:
            # They Double Clicked! Give them the cached answer
            return Response(cached_response['data'], status=cached_response['status'])


        # 1. Give the incoming JSON data to our bouncer (the Serializer)
        
        serializer = UserRegistrationSerializer(data=request.data)

        # 2. The bouncer checks if the data matches the blueprint perfeclty

        if serializer.is_valid():

            # 3. If valid, encrpyt the password and save to the database

            user = serializer.save()

            refresh = CustomLoginSerializer.get_token(user)

            response_data = {
                "message": "Account created successfully",
                "access": str(refresh.access_token),
                "refresh": str(refresh)
            }
            response_status = status.HTTP_201_CREATED
            
            # Trap the double-click: Save the answer in the cache for 24 hours
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
            
        # If invalid (e.g., missing an email), send the exact error back

        return Response(
            serializer.errors,
            status=status.HTTP_400_BAD_REQUEST
        )
    
    def throttled(self, request, wait):
        seconds = max(1, math.ceil(wait if wait is not None else 300))
        minutes = math.ceil(seconds / 60)
        unit = 'minute' if minutes == 1 else 'minutes'
        message = (
            'No account was created by this attempt. Too many registration attempts. '
            f'Please try again in {minutes} {unit}.'
        )
        error = Throttled(detail=message, code='registration_throttled')
        # Preserve Retry-After without adding DRF's technical seconds suffix.
        error.wait = seconds
        raise error


class LoginThrottle(AnonRateThrottle):
    """
    Implements a Sliding Window rate limit for login attempts.
    This throttle prevents brute-force attacks by limiting the number of 
    failed login attempts an anonymous user can make. It uses a sliding 
    window algorithm rather than a fixed clock, meaning the restriction 
    only lifts when the oldest failed attempt falls out of the time window.
    Attributes:
        rate (str): Set to 'custom' to bypass DRF's default s/m/h/d parser.
    """
    
    rate = 'custom'
    
    def parse_rate(self, rate):
        """
        Overrides the default string parser to enforce exact math.
        
        Returns:
            tuple: (number_of_attempts, cooldown_in_seconds)
                   Currently set to 5 attempts per 300 seconds (5 minutes).
        """
        return (5, 300)

class CustomLoginView(TokenObtainPairView):
    """
    Secure login endpoint utilizing JWT authentication and brute-force protection.
    Inherits from SimpleJWT's TokenObtainPairView to generate Access and 
    Refresh tokens. It applies a custom serializer to format error messages 
    and a rate throttle to lock out abusive traffic.
    Attributes:
        serializer_class (Serializer): Custom serializer for user-friendly 401 errors.
        throttle_classes (list): Applies the LoginThrottle sliding window limit.
    """
    serializer_class = CustomLoginSerializer
    throttle_classes = [LoginThrottle]
    def throttled(self, request, wait):
        """
        Intercepts the default throttle exception to provide a custom UI message.
        Args:
            request (Request): The incoming HTTP request.
            wait (int): The number of seconds remaining before the throttle lifts.
        Raises:
            Throttled: Returns a 429 Too Many Requests with a user-friendly 
                       wait time rounded up to the nearest minute.
        """
        custom_message = f"Too many attempts. Please try again in {math.ceil(wait/60)} minutes."
        raise Throttled(detail=custom_message)

from rest_framework import generics
from rest_framework.parsers import MultiPartParser, FormParser
from .serializers import ProfileImageUploadSerializer


class AdminUserListView(generics.ListAPIView):
    """
    Returns registered users formatted for the Admin User Directory dashboard.
    """
    permission_classes = [IsAuthenticated, IsPortalAdmin]

    def get(self, request, *args, **kwargs):
        role_param = request.query_params.get('role')
        barangay_param = request.query_params.get('barangay')
        manage_access = request.query_params.get('access_management') == '1'

        # Apply the shared saved-profile policy below; never infer jurisdiction from street text.
        from chat.services import can_chat, location_key
        from chat.moderation import may_manage
        is_barangay = request.user.account_type == 'Barangay'
        if is_barangay:
            barangay_param = None

        # Dynamic active LGU barangay set (accounts with account_type='Barangay')
        active_lgus = [
            b.strip() for b in tbl_user_profile.objects.filter(
                account_type='Barangay', is_active=True
            ).exclude(barangay__isnull=True).exclude(barangay__exact='')
            .values_list('barangay', flat=True) if b and b.strip()
        ]
        active_lgus_lower = [b.lower() for b in active_lgus]

        if manage_access:
            users_qs = tbl_user_profile.objects.filter(account_type__in=['Homeowner', 'Kasambahay']).prefetch_related('documents').order_by('first_name', 'last_name', 'pk')
        elif role_param and role_param.upper() in ['BARANGAY', 'ADMIN']:
            users_qs = tbl_user_profile.objects.filter(account_type='Barangay').order_by('barangay')
        elif role_param and role_param.upper() == 'SUPERADMIN':
            users_qs = tbl_user_profile.objects.filter(account_type='Admin').order_by('-date_joined')
        else:
            users_qs = tbl_user_profile.objects.filter(account_type__in=['Homeowner', 'Kasambahay', 'Barangay', 'Admin']).order_by('-date_joined')
            if role_param and role_param.upper() != 'ALL':
                users_qs = users_qs.filter(account_type__iexact=role_param)

        from django.db.models import Q
        if barangay_param and not manage_access:
            if barangay_param.upper() in ['UNASSIGNED', 'NO LGU COVERAGE', 'NO_LGU']:
                # Exclude all users that belong to active LGU barangays
                q_assigned = Q()
                for b in active_lgus:
                    q_assigned |= Q(barangay__iexact=b)
                users_qs = users_qs.exclude(q_assigned)
            elif barangay_param.upper() not in ['ALL', 'ALL BARANGAYS']:
                users_qs = users_qs.filter(
                    Q(account_type='Admin') |
                    Q(barangay__iexact=barangay_param) |
                    Q(city__icontains=barangay_param) |
                    Q(street__icontains=barangay_param)
                )

        data = []
        for u in users_qs:
            if manage_access:
                # Include suspended/deactivated residents for restoration, while enforcing
                # the same strict authority as the suspension mutation endpoint.
                if not may_manage(request.user, u):
                    continue
                if barangay_param and barangay_param.upper() not in ['ALL', 'ALL BARANGAYS']:
                    key = location_key(u.barangay, barangay=True)
                    if barangay_param.upper() in ['UNASSIGNED', 'NO LGU COVERAGE', 'NO_LGU']:
                        covered = {location_key(b, barangay=True) for b in active_lgus}
                        if key and key in covered:
                            continue
                    elif key != location_key(barangay_param, barangay=True):
                        continue
            elif is_barangay and not can_chat(request.user, u):
                continue
            if u.account_type == 'Barangay':
                full_name = f"Brgy. {u.barangay} Officer" if u.barangay else (f"{u.first_name} {u.last_name}".strip() or u.username)
                role_norm = 'BARANGAY'
            elif u.account_type == 'Admin':
                full_name = f"{u.first_name} {u.last_name}".strip() or "City Super Admin"
                role_norm = 'SUPERADMIN'
            elif u.account_type == 'Kasambahay':
                full_name = f"{u.first_name} {u.last_name}".strip() or u.username
                role_norm = 'KASAMBAHAY'
            else:
                full_name = f"{u.first_name} {u.last_name}".strip() or u.username
                role_norm = 'HOMEOWNER'

            avatar = (
                get_signed_cloudinary_url(u.profile_link, as_avatar=True)
                or f"https://ui-avatars.com/api/?name={u.first_name}+{u.last_name}&background=F5A623&color=fff"
            )
            is_verified = (u.verification_status == 'Verified') or (u.account_type in ['Barangay', 'Admin'])

            brgy = (u.barangay or '').strip()
            if not brgy and not manage_access:
                street_lower = (u.street or '').lower()
                city_lower = (u.city or '').lower()
                for b in active_lgus:
                    if b.lower() in street_lower or b.lower() in city_lower:
                        brgy = b
                        break
            if not brgy:
                brgy = 'Unassigned'

            has_lgu_coverage = bool(brgy and brgy.lower() in active_lgus_lower)

            user_item = {
                "id": str(u.id),
                "name": full_name,
                "role": role_norm,
                "avatar": avatar,
                "email": u.email,
                "contactNumber": u.contact_number or "+639123456789",
                "address": f"{u.street or ''}, {u.city or 'Cagayan de Oro City'}".strip(', '),
                "barangay": brgy,
                "city": u.city or "Cagayan de Oro City",
                "verified": is_verified,
                "hasLguCoverage": has_lgu_coverage,
                "status": "ACTIVE" if u.is_active and not u.is_account_suspended else "SUSPENDED",
                "canManageSuspension": may_manage(request.user, u),
                "joinedDate": u.date_joined.strftime('%b %d, %Y') if u.date_joined else "Recent",
                "completedJobs": 0,
                "sentimentScore": {
                    "positive": 95,
                    "neutral": 5,
                    "negative": 0
                },
                "ra10361Compliant": True,
            }
            if manage_access:
                user_item.update({
                    'chatSuspendedUntil': u.chat_suspended_until.isoformat() if u.chat_suspended_until else None,
                    'isAccountSuspended': u.is_account_suspended,
                    'isAccountActive': u.is_active,
                })
            social_links_raw = getattr(u, 'social_links', []) or []
            show_links = getattr(u, 'show_social_links', True)
            user_item["socialLinks"] = social_links_raw if show_links else []
            data.append(user_item)

        response = Response(data, status=status.HTTP_200_OK)
        if manage_access:
            response['Cache-Control'] = 'private, no-store'
        return response


class AdminDashboardStatsView(APIView):
    """One authoritative database snapshot; resident locations never imply LGU accounts."""
    permission_classes = [IsAuthenticated, IsPortalAdmin]

    def get(self, request, *args, **kwargs):
        from .admin_statistics import resident_snapshot, scope_for
        response = Response(resident_snapshot(scope_for(request), restrict_directory=request.user.account_type == 'Barangay'))
        response['Cache-Control'] = 'private, no-store'
        return response


class AdminDashboardActivityView(APIView):
    """
    Returns recent booking placements for the admin dashboard activity table.
    Queries real tbl_booking + tbl_booking_assignment records, scoped optionally by barangay.
    Returns the latest 10 accepted/in-progress/completed bookings.
    """
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        barangay_param = request.query_params.get('barangay')
        from booking.models import tbl_booking, tbl_booking_assignment
        from django.db.models import Q

        # Get bookings ordered by most recent first
        bookings_qs = tbl_booking.objects.select_related('poster_id').order_by('-createdAt')

        # Optional barangay scope
        if barangay_param and barangay_param.upper() not in ['ALL', 'ALL BARANGAYS']:
            bookings_qs = bookings_qs.filter(
                Q(barangay__iexact=barangay_param) |
                Q(barangay__icontains=barangay_param) |
                Q(poster_id__barangay__iexact=barangay_param) |
                Q(poster_id__barangay__icontains=barangay_param) |
                Q(poster_id__city__icontains=barangay_param) |
                Q(poster_id__street__icontains=barangay_param)
            )

        result = []
        for booking in bookings_qs[:100]:
            poster = booking.poster_id
            poster_name = f"{poster.first_name} {poster.last_name}".strip() or poster.username
            poster_role = (getattr(poster, 'account_type', '') or '').strip()

            # Build avatar URL for poster (Optimized WebP)
            poster_avatar = (
                get_signed_cloudinary_url(poster.profile_link, as_avatar=True)
                or f"https://ui-avatars.com/api/?name={poster.first_name}+{poster.last_name}&background=F5A623&color=fff"
            )

            # Try to find assigned counterpart
            assignment = tbl_booking_assignment.objects.filter(booking_id=booking).select_related('accepter_id').first()
            accepter_name = None
            accepter_avatar = None
            if assignment and assignment.accepter_id:
                w = assignment.accepter_id
                accepter_name = f"{w.first_name} {w.last_name}".strip() or w.username
                accepter_avatar = (
                    get_signed_cloudinary_url(w.profile_link, as_avatar=True)
                    or f"https://ui-avatars.com/api/?name={w.first_name}+{w.last_name}&background=0D0D11&color=fff"
                )

            unassigned_avatar = 'https://ui-avatars.com/api/?name=?&background=E2E8F0&color=94A3B8'

            # Role-aware assignment:
            # If poster is Kasambahay (offering services / looking for job), Kasambahay is the worker.
            # The Employer is the counterpart who hired/accepted them (or 'Unassigned').
            if poster_role.lower() == 'kasambahay':
                worker_name = poster_name
                worker_avatar = poster_avatar
                homeowner_name = accepter_name or 'Unassigned'
                homeowner_avatar = accepter_avatar or unassigned_avatar
            else:
                # Homeowner posted the job request, Kasambahay is the counterpart (or 'Unassigned')
                homeowner_name = poster_name
                homeowner_avatar = poster_avatar
                worker_name = accepter_name or 'Unassigned'
                worker_avatar = accepter_avatar or unassigned_avatar

            from booking.wage_policy import get_minimum_wage_info
            from booking.services import pair_booking_count
            from rest_framework.exceptions import ValidationError
            try:
                policy = get_minimum_wage_info(booking.booking_type, booking.full_address, booking.region)
                minimum = float(policy['minimum_rate'])
            except ValidationError:
                minimum = None
            amount = float(booking.rate_amount)
            needs_review = booking.rate_needs_review or minimum is None
            is_below_min = minimum is not None and amount < minimum
            short_term_count = pair_booking_count(poster.pk, assignment.accepter_id_id, booking.start_time) if assignment else 0
            is_capped = short_term_count >= 3
            if needs_review:
                compliance_status = 'REQUIRES_REVIEW'
            elif is_below_min:
                compliance_status = 'BELOW_MINIMUM_WAGE'
            elif short_term_count > 3 and booking.booking_type == 'short_term':
                compliance_status = 'FLAGGED_THROTTLED'
            else:
                compliance_status = 'COMPLIANT'

            # Contract type label
            contract_type = 'Formal Kasambahay (Long-Term)' if booking.booking_type == 'long_term' else 'Short-Term On-Demand'

            # Barangay
            brgy = booking.barangay or poster.barangay or ''
            if not brgy:
                for b_name in ['Pagatpat', 'Canitoan']:
                    if b_name.lower() in (poster.street or '').lower() or b_name.lower() in (poster.city or '').lower() or b_name.lower() in (booking.street or '').lower() or b_name.lower() in (booking.full_address or '').lower():
                        brgy = b_name
                        break
            if not brgy:
                brgy = 'Unknown'

            result.append({
                'id': str(booking.booking_id),
                'homeownerName': homeowner_name,
                'homeownerAvatar': homeowner_avatar,
                'workerName': worker_name,
                'workerAvatar': worker_avatar,
                'serviceCategory': ', '.join(booking.service_category),
                'monthlyBookingsCount': short_term_count,
                'isCapped': is_capped,
                'offeredWage': amount,
                'ratePeriod': booking.rate_period,
                'pairKey': ':'.join(sorted([str(poster.pk), str(assignment.accepter_id_id)])) if assignment else None,
                'rateNeedsReview': needs_review,
                'dailyRate': amount if booking.rate_period == 'day' else None,
                'minimumWageBaseline': minimum,
                'isBelowMinimumWage': is_below_min,
                'contractType': contract_type,
                'bookingType': booking.booking_type,
                'bookingStatus': booking.booking_status,
                'startDate': booking.start_time.strftime('%b %d, %Y') if booking.start_time else 'TBD',
                'status': compliance_status,
                'barangay': brgy,
                'createdAt': booking.createdAt.isoformat() if booking.createdAt else None,
            })

        return Response({'bookings': result, 'count': len(result)}, status=status.HTTP_200_OK)


class AdminMonthlyTrendView(APIView):
    """Verified workforce as of each Philippine month end, from status events."""
    permission_classes = [IsAuthenticated, IsPortalAdmin]

    def get(self, request, *args, **kwargs):
        from .admin_statistics import resident_snapshot, scope_for, matches_scope
        from .workforce import monthly_workforce
        scope = scope_for(request)
        metrics = resident_snapshot(scope)['metrics']
        response = Response(monthly_workforce(scope, metrics, matches_scope))
        response['Cache-Control'] = 'private, no-store'
        return response


class AdminVerificationStatusStatsView(APIView):
    """One account per status; each role requires its complete current credentials."""
    permission_classes = [IsAuthenticated, IsPortalAdmin]

    def get(self, request, *args, **kwargs):
        from .admin_statistics import scope_for, matches_scope
        from .workforce import credential_snapshot
        from rest_framework.exceptions import ValidationError
        scope = scope_for(request)
        role = request.query_params.get('role', 'All Roles')
        if role not in ('All Roles', 'Kasambahay', 'Homeowner'):
            raise ValidationError({'role': 'Choose All Roles, Kasambahay or Homeowner.'})
        users = tbl_user_profile.objects.filter(account_type__in=['Kasambahay', 'Homeowner'], is_active=True).prefetch_related('documents')
        if role != 'All Roles':
            users = users.filter(account_type=role)
        counts = dict(verified=0, pending=0, unverified=0, rejected=0, expired=0)
        for user in users:
            if matches_scope(user.barangay, scope):
                counts[credential_snapshot(user)[0]] += 1
        response = Response({'barangay': scope, 'role': role, 'stats': counts, 'total': sum(counts.values())})
        response['Cache-Control'] = 'private, no-store'
        return response


class AdminLoginView(APIView):
    """
    Dedicated authentication endpoint for Web Admin Portal (Superadmin and Barangay LGU officers).
    Allows any user with account_type in ['Admin', 'Barangay'] or is_staff/is_superuser to log in.
    Returns their profile, role, and assigned barangay.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        raw_identifier = (request.data.get('username') or request.data.get('email') or '').strip()

        password = (request.data.get('password') or '').strip()

        if not raw_identifier or not password:
            return Response({'error': 'Please provide both username/email and password.'}, status=status.HTTP_400_BAD_REQUEST)

        from django.db.models import Q
        user = tbl_user_profile.objects.filter(
            Q(email__iexact=raw_identifier) | Q(username__iexact=raw_identifier)
        ).first()

        if user is None or not user.check_password(password):
            return Response({'error': 'Invalid credentials. Please check your username and password.'}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_active:
            return Response({'error': 'This administrative account has been deactivated.'}, status=status.HTTP_403_FORBIDDEN)

        from .authentication import assert_account_available
        assert_account_available(user)

        if user.account_type == 'Barangay':
            # Guard: LGU account must have a barangay assigned or login is blocked
            if not user.barangay or not user.barangay.strip():
                return Response(
                    {'error': 'Your LGU account is not assigned to a barangay. Please contact the Superadmin to fix your account.'},
                    status=status.HTTP_403_FORBIDDEN
                )
            role = 'ADMIN'
            barangay = user.barangay.strip().title()
        elif user.account_type == 'Admin' or user.is_superuser or user.is_staff:
            role = 'SUPERADMIN'
            barangay = 'All Barangays'
        else:
            return Response({'error': 'Access denied. Only Superadmins and Barangay Officers can access this portal.'}, status=status.HTTP_403_FORBIDDEN)

        from rest_framework_simplejwt.tokens import RefreshToken
        refresh = RefreshToken.for_user(user)

        avatar = (
            get_signed_cloudinary_url(user.profile_link, as_avatar=True)
            or f"https://ui-avatars.com/api/?name={user.first_name}+{user.last_name}&background=0D0D11&color=fff"
        )

        return Response({
            'success': True,
            'token': str(refresh.access_token),
            'refresh': str(refresh),
            'user': {
                'id': str(user.id),
                'username': user.username,
                'name': f"{user.first_name} {user.last_name}".strip() or user.username,
                'email': user.email,
                'role': role,
                'barangay': barangay,
                'avatar': avatar,
            }
        }, status=status.HTTP_200_OK)


class AdminActiveBarangaysView(APIView):
    """Real LGU accounts and registered resident locations, with no seeded jurisdictions."""
    permission_classes = [IsAuthenticated, IsPortalAdmin]

    def get_permissions(self):
        classes = [IsAuthenticated, IsPortalSuperadmin] if self.request.method == 'POST' else self.permission_classes
        return [permission() for permission in classes]

    def get(self, request):
        from .admin_statistics import resident_snapshot, scope_for
        rows = resident_snapshot(scope_for(request), restrict_directory=request.user.account_type == 'Barangay')['barangays']
        active = [row['name'] for row in rows if row['status'] == 'ACTIVE']
        response = Response({'barangays': active, 'active_lgus': active,
                             'user_barangays': [row['name'] for row in rows]})
        response['Cache-Control'] = 'private, no-store'
        return response

    def post(self, request):
        """
        Registers a new official administrative Barangay LGU jurisdiction in the database.
        Creates an active Barangay user account in tbl_user_profile, ensuring strict persistence.
        """
        data = request.data
        raw_name = data.get('name') or data.get('barangay')
        if not raw_name or not str(raw_name).strip():
            return Response({'error': 'Barangay name is required.'}, status=status.HTTP_400_BAD_REQUEST)

        clean_b = re.sub(r'^(brgy\.?|barangay)\s+', '', str(raw_name), flags=re.IGNORECASE).strip().title()

        # Check for duplicate Barangay account
        existing = tbl_user_profile.objects.filter(
            account_type='Barangay',
            is_active=True
        )
        for ex in existing:
            ex_clean = re.sub(r'^(brgy\.?|barangay)\s+', '', ex.barangay or '', flags=re.IGNORECASE).strip().lower()
            if ex_clean == clean_b.lower():
                return Response(
                    {'error': f"An official LGU account for Barangay {clean_b} is already registered in the directory."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        slug = re.sub(r'[^a-z0-9]', '', clean_b.lower())
        email = data.get('email') or f"{slug}@lgu.serbisure.ph"
        if tbl_user_profile.objects.filter(email=email).exists():
            import uuid
            email = f"{slug}_{str(uuid.uuid4())[:4]}@lgu.serbisure.ph"

        contact_number = (data.get('contact_number') or '').strip()
        if contact_number:
            clean_digits = re.sub(r'\D', '', contact_number)
            if clean_digits.startswith('639') and len(clean_digits) == 12:
                contact_number = f"+{clean_digits}"
            elif clean_digits.startswith('09') and len(clean_digits) == 11:
                contact_number = f"+63{clean_digits[1:]}"
            elif clean_digits.startswith('9') and len(clean_digits) == 10:
                contact_number = f"+63{clean_digits}"
            else:
                return Response(
                    {'error': "Please enter a valid 10-digit mobile number starting with 9 (e.g. 917 123 4567)."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Check if this contact number is already registered in the system
            if tbl_user_profile.objects.filter(contact_number=contact_number).exists():
                display_num = f"+63 {contact_number[3:6]} {contact_number[6:9]} {contact_number[9:]}" if len(contact_number) == 13 else contact_number
                return Response(
                    {'error': f"The contact number {display_num} is already registered to another account. Please use a different hotline number."},
                    status=status.HTTP_400_BAD_REQUEST
                )

        if not contact_number:
            import hashlib
            base_h = int(hashlib.md5(f"lgu_{clean_b}".encode('utf-8')).hexdigest()[:8], 16) % 900000000 + 100000000
            contact_number = f"+639{base_h}"
            attempts = 0
            while tbl_user_profile.objects.filter(contact_number=contact_number).exists() and attempts < 20:
                base_h = (base_h + 1) % 900000000 + 100000000
                contact_number = f"+639{base_h}"
                attempts += 1

        try:
            user = tbl_user_profile(
                username=f"lgu_{slug}",
                email=email,
                first_name=clean_b,
                last_name="Desk",
                account_type="Barangay",
                barangay=clean_b,
                region=data.get('region') or 'Region X - Northern Mindanao',
                province=data.get('province') or 'Misamis Oriental',
                city=data.get('city') or 'City of Cagayan De Oro',
                street=data.get('street') or '',
                zipcode=data.get('zipcode') or '9000',
                country=data.get('country') or 'Philippines',
                contact_number=contact_number,
                is_active=True,
                is_staff=True,
            )
            user.set_password(data.get('password') or 'Lgu@12345')
            user.save()
        except IntegrityError as e:
            err_str = str(e).lower()
            if 'contact_number' in err_str:
                display_num = f"+63 {contact_number[3:6]} {contact_number[6:9]} {contact_number[9:]}" if len(contact_number) == 13 else contact_number
                error_msg = f"The contact number {display_num} is already registered to another account. Please use a different hotline number."
            elif 'unique_lgu_account_per_barangay' in err_str:
                error_msg = f"Barangay {clean_b} is already registered in the directory. Each barangay can only have one official account."
            elif 'email' in err_str:
                error_msg = f"The email address '{email}' is already in use by another account. Please use a different email."
            elif 'username' in err_str:
                error_msg = f"The username 'lgu_{slug}' is already taken. Please try again."
            else:
                error_msg = f"Barangay {clean_b} could not be registered because an account with matching details already exists."
            return Response(
                {'error': error_msg},
                status=status.HTTP_400_BAD_REQUEST
            )
        except Exception:
            return Response(
                {'error': f"Unable to register Barangay {clean_b}. Please verify the form details and try again."},
                status=status.HTTP_400_BAD_REQUEST
            )

        return Response({
            'success': True,
            'message': f"Barangay {clean_b} successfully registered in the database.",
            'barangay': {
                'name': clean_b,
                'email': user.email,
                'barangay': clean_b,
                'city': user.city,
                'province': user.province,
                'region': user.region,
                'zipcode': user.zipcode,
                'country': user.country,
                'status': 'ACTIVE',
            }
        }, status=status.HTTP_201_CREATED)


class ProfileImageUploadThrottle(UserRateThrottle):
    scope = 'profile_image_upload'
    rate = '2/h'

class ProfileImageUploadView(generics.UpdateAPIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)
    serializer_class = ProfileImageUploadSerializer
    throttle_classes = [ProfileImageUploadThrottle]

    def perform_update(self, serializer):
        super().perform_update(serializer)
        self.upload_timings = serializer.context.get('upload_timings', {})

    def update(self, request, *args, **kwargs):
        from time import perf_counter
        started_at = perf_counter()
        response = super().update(request, *args, **kwargs)
        timings = {**getattr(self, 'upload_timings', {}), 'profile_request': (perf_counter() - started_at) * 1000}
        response['Server-Timing'] = ', '.join(f'{name};dur={duration:.1f}' for name, duration in timings.items())
        return response

    def get_object(self):
        # We automatically return the logged-in user!
        return self.request.user

    def throttled(self, request, wait):        
    # 3600 seconds = 1 hour
        if wait > 3600: 
            time_left = math.ceil(wait / 3600)
            custom_message = f"Too many attempts. Please try again in {time_left} hours."

        else:
            custom_message = f"Too many attempts. Please try again in {math.ceil(wait/60)} minutes."

        raise Throttled(detail=custom_message)


class UserAboutThrottle(UserRateThrottle):
    scope = 'user_about'
    rate = '3/h'

class UserAboutView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = UserAboutSerializer

    def get_throttles(self):
        if self.request.method in ['PATCH', 'PUT']:
            return [UserAboutThrottle()]
        return []

    def get_object(self):
        return self.request.user

    def throttled(self, request, wait):        
        # 3600 seconds = 1 hour
        if wait > 3600: 
            time_left = math.ceil(wait / 3600)
            custom_message = f"Too many attempts. Please try again in {time_left} hours."

        else:
            custom_message = f"Too many attempts. Please try again in {math.ceil(wait/60)} minutes."

        raise Throttled(detail=custom_message)

class UserTagsThrottle(UserRateThrottle):
    scope = 'user_tags'
    rate = '60/h'

class UserTagsView(generics.RetrieveUpdateAPIView):

    permission_classes = [IsAuthenticated]
    serializer_class = UserTagsSerializer
    throttle_classes = [UserTagsThrottle]

    def get_object(self):
        return self.request.user
    
    def throttled(self, request, wait):        
        # 3600 seconds = 1 hour
        if wait > 3600: 
            time_left = math.ceil(wait / 3600)
            custom_message = f"Too many attempts. Please try again in {time_left} hours."

        else:
            custom_message = f"Too many attempts. Please try again in {math.ceil(wait/60)} minutes."

        raise Throttled(detail=custom_message)


class ContactPrivacyView(generics.RetrieveUpdateAPIView):
    """
    Get or update contact number visibility for the authenticated user.
    GET/PATCH /api/v1/accounts/contact-privacy/
    """
    permission_classes = [IsAuthenticated]
    serializer_class = ContactPrivacySerializer

    def get_object(self):
        return self.request.user


class UserSocialLinksThrottle(UserRateThrottle):
    scope = 'user_social_links'
    rate = '30/h'


class UserSocialLinksView(generics.RetrieveUpdateAPIView):
    """
    Get (GET) or update (PATCH, PUT) social accounts and contact links for the authenticated user.
    GET/PATCH /api/v1/accounts/social-links/
    """
    permission_classes = [IsAuthenticated]
    serializer_class = UserSocialLinksSerializer
    throttle_classes = [UserSocialLinksThrottle]

    def get_object(self):
        return self.request.user

    def throttled(self, request, wait):
        if wait > 3600:
            time_left = math.ceil(wait / 3600)
            custom_message = f"Too many attempts. Please try again in {time_left} hours."
        else:
            custom_message = f"Too many attempts. Please try again in {math.ceil(wait / 60)} minutes."
        raise Throttled(detail=custom_message)


class JobStatusView(generics.RetrieveUpdateAPIView):
    """
    Get or update Kasambahay availability / job status.
    GET/PATCH /api/v1/accounts/job-status/
    """
    permission_classes = [IsAuthenticated, IsKasambahay]
    serializer_class = JobStatusSerializer

    def get_object(self):
        return self.request.user

    def perform_update(self, serializer):
        from django.db import transaction
        with transaction.atomic():
            # Serialize concurrent mobile updates; the save and history commit together.
            serializer.instance = tbl_user_profile.objects.select_for_update().get(pk=self.request.user.pk)
            serializer.save()

class PublicProfileView(generics.RetrieveAPIView):
    """
    Read-only public profile for any user by UUID.
    Only accessible by authenticated users.
    """
    permission_classes = [IsAuthenticated]
    serializer_class = PublicProfileSerializer
    lookup_field = 'id'
    queryset = tbl_user_profile.objects.filter(is_active=True)


class KasambahayResumeView(generics.RetrieveAPIView):
    """Owner-only access to old uploaded files; new CVs use worker-cv exports."""
    permission_classes = [IsAuthenticated, IsKasambahay]
    serializer_class = KasambahayResumeSerializer
    http_method_names = ['get', 'head', 'options']

    def get_object(self):
        return self.request.user


class ChangePasswordView(APIView):
    """
    Endpoint allowing authenticated users to change their password.
    Requires current_password, new_password, and confirm_password.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current_password = request.data.get('current_password')
        new_password = request.data.get('new_password')
        confirm_password = request.data.get('confirm_password')

        if not current_password or not new_password or not confirm_password:
            return Response(
                {'error': 'Current password, new password, and confirmation are required.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        if new_password != confirm_password:
            return Response({'error': 'New passwords do not match.'}, status=status.HTTP_400_BAD_REQUEST)

        if len(new_password) < 8:
            return Response({'error': 'Password must be at least 8 characters long.'}, status=status.HTTP_400_BAD_REQUEST)

        if not request.user.check_password(current_password):
            return Response({'error': 'Current password is incorrect.'}, status=status.HTTP_400_BAD_REQUEST)

        if current_password == new_password:
            return Response({'error': 'New password must be different from current password.'}, status=status.HTTP_400_BAD_REQUEST)

        request.user.set_password(new_password)
        request.user.save()
        return Response({'message': 'Password changed successfully.'}, status=status.HTTP_200_OK)


class UserSearchView(APIView):
    """
    Search active users by query keyword, account type (Kasambahay / Homeowner),
    city, province, or tags (Tier 2-2).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        query = request.query_params.get('q', '').strip()
        role = request.query_params.get('role', '').strip()
        location = request.query_params.get('location', '').strip()
        tag = request.query_params.get('tag', '').strip()

        qs = tbl_user_profile.objects.filter(is_active=True).exclude(id=request.user.id)

        if role:
            qs = qs.filter(account_type__iexact=role)
        
        if location:
            qs = qs.filter(Q(city__icontains=location) | Q(province__icontains=location) | Q(street__icontains=location))

        if tag:
            qs = qs.filter(user_tags__icontains=tag)

        if query:
            qs = qs.filter(
                Q(first_name__icontains=query) |
                Q(last_name__icontains=query) |
                Q(user_about__icontains=query) |
                Q(city__icontains=query) |
                Q(user_tags__icontains=query)
            )

        serializer = PublicProfileSerializer(qs[:30], many=True)
        return Response({'users': serializer.data, 'count': qs.count()}, status=status.HTTP_200_OK)


class DeleteAccountView(APIView):
    """
    Soft-deletes and deactivates the authenticated user's account (Tier 3-5).
    Requires the current password to confirm the critical operation.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        password = request.data.get('password')
        if not password:
            return Response({'error': 'Password is required to confirm account deletion.'}, status=status.HTTP_400_BAD_REQUEST)

        if not request.user.check_password(password):
            return Response({'error': 'Incorrect password.'}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        user.is_active = False
        user.user_about = '[Account Deactivated]'
        # Anonymize contact number collision-free using user.id hash while respecting ^\+639\d{9}$
        unique_suffix = f"{int(user.id.hex[:8], 16) % 900000000 + 100000000}"
        user.contact_number = f"+639{unique_suffix}"
        user.user_tags = []
        user.save()

        return Response({'message': 'Your account has been deactivated successfully.'}, status=status.HTTP_200_OK)


class ExportUserDataView(APIView):
    """
    Exports a GDPR-style archive of the user's personal data (Tier 3-5).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        from booking.models import tbl_booking, tbl_booking_assignment
        from reviews.models import tbl_review

        posted_bookings = list(tbl_booking.objects.filter(poster_id=user).values(
            'booking_id', 'booking_type', 'booking_status', 'service_category', 'rate_amount', 'rate_period', 'rate_needs_review',
            'street', 'barangay', 'city', 'province', 'region', 'zip_code', 'createdAt'
        ))
        for b in posted_bookings:
            b['booking_id'] = str(b['booking_id'])
            b['createdAt'] = str(b['createdAt'])
            b['rate_amount'] = str(b['rate_amount'])
            addr_parts = [b.get(k) for k in ['street', 'barangay', 'city', 'province'] if b.get(k)]
            b['full_address'] = ', '.join(addr_parts) if addr_parts else ''

        assigned_bookings = list(tbl_booking_assignment.objects.filter(accepter_id=user).values(
            'booking_assignment_id', 'booking_id', 'accepted_at'
        ))
        for a in assigned_bookings:
            a['booking_assignment_id'] = str(a['booking_assignment_id'])
            a['booking_id'] = str(a['booking_id'])
            a['accepted_at'] = str(a['accepted_at'])

        reviews_given = list(tbl_review.objects.filter(reviewer_id=user).values(
            'review_id', 'rating', 'unstructured_feedback', 'nlp_sentiment', 'createdAt'
        ))
        for r in reviews_given:
            r['review_id'] = str(r['review_id'])
            r['createdAt'] = str(r['createdAt'])

        reviews_received = list(tbl_review.objects.filter(reviewee_id=user).values(
            'review_id', 'rating', 'unstructured_feedback', 'nlp_sentiment', 'createdAt'
        ))
        for r in reviews_received:
            r['review_id'] = str(r['review_id'])
            r['createdAt'] = str(r['createdAt'])

        data = {
            'profile': {
                'id': str(user.id),
                'email': user.email,
                'first_name': user.first_name,
                'middle_name': user.middle_name,
                'last_name': user.last_name,
                'account_type': user.account_type,
                'verification_status': user.verification_status,
                'contact_number': user.contact_number,
                'user_about': user.user_about,
                'user_tags': user.user_tags,
                'city': user.city,
                'province': user.province,
                'date_joined': str(user.date_joined),
            },
            'posted_bookings': posted_bookings,
            'assigned_bookings': assigned_bookings,
            'reviews_given': reviews_given,
            'reviews_received': reviews_received,
        }

        return Response({'user_data': data}, status=status.HTTP_200_OK)


class AdminBarangayDeskProfileView(APIView):
    """
    Dedicated endpoint for Barangay Desk Profile in the Admin Portal.
    Allows authenticated LGU officers (or Superadmins) to view and update
    the desk information stored across existing tbl_user_profile columns:
    - barangay: user's barangay name
    - street: desk / barangay hall physical address
    - contact_number: official desk hotline
    - user_about: desk operating hours & public information
    - first_name & last_name: desk officer-in-charge
    - email: desk official email
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        barangay_param = request.query_params.get('barangay')

        # If Superadmin specifies ?barangay=..., look up that LGU officer
        if user.account_type in ['Admin'] or user.is_staff or user.is_superuser:
            if barangay_param and barangay_param.lower() not in ['all', 'all barangays']:
                target_user = tbl_user_profile.objects.filter(
                    account_type='Barangay',
                    barangay__iexact=barangay_param.strip()
                ).first()
                if target_user:
                    user = target_user

        officer_name = f"{user.first_name} {user.last_name}".strip()

        return Response({
            'success': True,
            'barangay': user.barangay or '',
            'street': user.street or '',
            'contact_number': user.contact_number or '',
            'user_about': user.user_about or '',
            'first_name': user.first_name or '',
            'last_name': user.last_name or '',
            'officer_name': officer_name,
            'email': user.email or '',
            'city': user.city or 'Cagayan de Oro City',
            'province': user.province or 'Misamis Oriental',
            'zipcode': user.zipcode or '9000',
        }, status=status.HTTP_200_OK)

    def put(self, request):
        return self.patch(request)

    def patch(self, request):
        user = request.user
        barangay_param = request.data.get('barangay')

        # Allow Superadmin to update a specific barangay's officer profile
        if (user.account_type in ['Admin'] or user.is_staff or user.is_superuser) and barangay_param:
            target_user = tbl_user_profile.objects.filter(
                account_type='Barangay',
                barangay__iexact=barangay_param.strip()
            ).first()
            if target_user:
                user = target_user

        data = request.data
        update_fields = []

        # 1. Street (Address)
        if 'street' in data:
            street_val = (data.get('street') or '').strip()
            if len(street_val) > 100:
                return Response({'error': 'Address cannot exceed 100 characters.'}, status=status.HTTP_400_BAD_REQUEST)
            user.street = street_val
            update_fields.append('street')

        # 2. Contact Number (Hotline)
        if 'contact_number' in data:
            raw_contact = (data.get('contact_number') or '').strip()
            if raw_contact:
                normalized = normalize_ph_phone_number(raw_contact)
                if not normalized or len(normalized) != 13 or not normalized.startswith('+639'):
                    return Response({'error': "Hotline must be a valid PH mobile number starting with '+639' (e.g., +639123456789)."}, status=status.HTTP_400_BAD_REQUEST)
                # Check uniqueness against other users
                conflict = tbl_user_profile.objects.filter(contact_number=normalized).exclude(id=user.id).exists()
                if conflict:
                    return Response({'error': 'This contact number is already registered by another account.'}, status=status.HTTP_400_BAD_REQUEST)
                user.contact_number = normalized
                update_fields.append('contact_number')

        # 3. User About (Office Hours & Desk Info)
        if 'user_about' in data:
            about_val = (data.get('user_about') or '').strip()
            if len(about_val) > 500:
                return Response({'error': 'Office hours & desk info cannot exceed 500 characters.'}, status=status.HTTP_400_BAD_REQUEST)
            user.user_about = about_val
            update_fields.append('user_about')

        # 4. Officer First Name & Last Name
        if 'first_name' in data:
            fn_val = (data.get('first_name') or '').strip()
            if fn_val:
                user.first_name = fn_val[:100]
                update_fields.append('first_name')

        if 'last_name' in data:
            ln_val = (data.get('last_name') or '').strip()
            if ln_val:
                user.last_name = ln_val[:100]
                update_fields.append('last_name')

        if update_fields:
            user.save(update_fields=list(set(update_fields)))

        officer_name = f"{user.first_name} {user.last_name}".strip()

        return Response({
            'success': True,
            'message': 'Barangay desk profile updated successfully.',
            'profile': {
                'barangay': user.barangay or '',
                'street': user.street or '',
                'contact_number': user.contact_number or '',
                'user_about': user.user_about or '',
                'first_name': user.first_name or '',
                'last_name': user.last_name or '',
                'officer_name': officer_name,
                'email': user.email or '',
                'city': user.city or 'Cagayan de Oro City',
                'province': user.province or 'Misamis Oriental',
                'zipcode': user.zipcode or '9000',
            }
        }, status=status.HTTP_200_OK)
