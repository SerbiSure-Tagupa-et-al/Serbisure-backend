from rest_framework import serializers
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle

from .app_releases import ReleaseUnavailable, latest_release


class AppVersionSerializer(serializers.Serializer):
    latest_version = serializers.CharField(allow_null=True)
    release_notes = serializers.CharField(allow_blank=True)
    apk_url = serializers.URLField(allow_null=True)
    apk_size = serializers.IntegerField(allow_null=True)


class AppVersionThrottle(AnonRateThrottle):
    rate = '30/min'


@api_view(['GET'])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([AppVersionThrottle])
def app_version(request):
    try:
        release = latest_release()
    except ReleaseUnavailable:
        return Response({'detail': 'Update information is unavailable. Please try again.'},
                        status=503, headers={'Retry-After': '30', 'Cache-Control': 'no-store'})
    return Response(AppVersionSerializer(release).data, headers={'Cache-Control': 'no-store'})
