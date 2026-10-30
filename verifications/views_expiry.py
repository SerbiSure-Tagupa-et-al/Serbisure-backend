from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsPortalAdmin
from verifications.services.expiry import officer_scope, preview_expirations, reject_expired_clearances


class ExpiryConfirmationSerializer(serializers.Serializer):
    confirmation_token = serializers.CharField(max_length=2048)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) - {'confirmation_token'}:
            raise serializers.ValidationError({'non_field_errors': ['Only confirmation_token is accepted; jurisdiction is determined by your account.']})
        return super().to_internal_value(data)


class ExpiryActionThrottle(UserRateThrottle):
    rate = '60/h'


class ExpiredClearancesView(APIView):
    permission_classes = [IsAuthenticated, IsPortalAdmin]
    throttle_classes = [ExpiryActionThrottle]

    def get(self, request):
        response = Response(preview_expirations(request.user))
        response['Cache-Control'] = 'private, no-store'
        return response

    def post(self, request):
        officer_scope(request.user)
        serializer = ExpiryConfirmationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(reject_expired_clearances(request.user, serializer.validated_data['confirmation_token'], request))
