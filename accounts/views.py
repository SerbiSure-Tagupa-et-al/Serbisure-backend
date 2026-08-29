from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from .serializers import UserRegistrationSerializer, CustomLoginSerializer, UserAboutSerializer, UserTagsSerializer
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.exceptions import Throttled
from django.core.cache import cache
from core.utils import check_valid_uuid
import math
from drf_spectacular.utils import extend_schema, OpenApiParameter
from drf_spectacular.types import OpenApiTypes

class RegistrationThrottle(AnonRateThrottle):
    rate = '5/d'

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

            from rest_framework_simplejwt.tokens import RefreshToken
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
        # 3600 seconds = 1 hour
        if wait > 3600: 
            time_left = math.ceil(wait / 3600)
            custom_message = f"Too many attempts. Please try again in {time_left} hours."

        else:
            custom_message = f"Too many attempts. Please try again in {math.ceil(wait/60)} minutes."

        raise Throttled(detail=custom_message)


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
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from .serializers import ProfileImageUploadSerializer


class ProfileImageUploadThrottle(UserRateThrottle):
    scope = 'profile_image_upload'
    rate = '2/h'

class ProfileImageUploadView(generics.UpdateAPIView):
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)
    serializer_class = ProfileImageUploadSerializer
    throttle_classes = [ProfileImageUploadThrottle]

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
    rate = '3/h'

class UserTagsView(generics.UpdateAPIView):

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
