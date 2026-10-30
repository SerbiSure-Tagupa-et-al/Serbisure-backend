from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from accounts.permissions import IsPortalAdmin
from verifications.serializers_corrections import ReviewerCorrectionSerializer
from verifications.services.corrections import correction_details, editable_document, save_corrections


class CorrectionThrottle(UserRateThrottle):
    rate = '120/h'


class ReviewerCorrectionsView(APIView):
    permission_classes = [IsAuthenticated, IsPortalAdmin]
    throttle_classes = [CorrectionThrottle]

    def get(self, request, document_id):
        response = Response(correction_details(editable_document(request.user, document_id)))
        response['Cache-Control'] = 'private, no-store'
        return response

    def patch(self, request, document_id):
        # Check jurisdiction before exposing validation details.
        editable_document(request.user, document_id)
        serializer = ReviewerCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(save_corrections(request.user, document_id, serializer.validated_data, request))
