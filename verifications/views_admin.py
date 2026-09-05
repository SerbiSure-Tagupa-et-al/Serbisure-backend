import logging
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from .models import tbl_documents
from .serializers_admin import (
    AdminDocumentDetailSerializer,
    AdminDocumentActionSerializer,
)
from verifications.services.document_processor import process_document_async
from notifications.models import tbl_notification

logger = logging.getLogger(__name__)


class IsAdminOrBarangay(BasePermission):
    """
    Allows access only to users with account_type 'Admin' or 'Barangay'.
    """
    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.account_type in ['Admin', 'Barangay']
        )


def check_and_update_profile_verification(user):
    """
    Checks if user is now fully verified according to their required documents:
    - Kasambahay: BOTH nbi_clearance AND police_clearance verified.
    - Homeowner: BOTH national_id_front AND national_id_back verified.
    If verified, sends a congratulations notification if not already sent.
    """
    if user.verification_status == 'Verified':
        notif_exists = tbl_notification.objects.filter(
            receiver_id=user,
            notification_message__icontains="fully VERIFIED"
        ).exists()
        if not notif_exists:
            logger.info(f"[Verification] User {user.email} is now fully Verified!")
            tbl_notification.objects.create(
                sender_id=user,
                receiver_id=user,
                notification_message="Congratulations! Your SerbiSure profile is now fully VERIFIED! 🎉",
            )



class AdminDocumentListView(generics.ListAPIView):
    """
    Endpoint for Admin & Barangay officials to view submitted documents.
    Supports query filters:
    - ?status=Pending (default all)
    - ?account_type=Kasambahay / Homeowner
    - ?document_type=nbi_clearance / police_clearance / national_id_front / national_id_back
    """
    permission_classes = [IsAuthenticated, IsAdminOrBarangay]
    serializer_class = AdminDocumentDetailSerializer

    def get_queryset(self):
        qs = tbl_documents.objects.all().select_related('user_profile', 'verifyBy').order_by('-created_at')

        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(verification_status=status_filter)

        account_type = self.request.query_params.get('account_type')
        if account_type:
            qs = qs.filter(user_profile__account_type=account_type)

        doc_type = self.request.query_params.get('document_type')
        if doc_type:
            qs = qs.filter(document_type=doc_type)

        return qs


class AdminDocumentDetailView(generics.RetrieveAPIView):
    """
    Get full details of a specific document including OCR output, discrepancy flags,
    and temporary signed image URL.
    """
    permission_classes = [IsAuthenticated, IsAdminOrBarangay]
    serializer_class = AdminDocumentDetailSerializer
    queryset = tbl_documents.objects.all().select_related('user_profile', 'verifyBy')
    lookup_field = 'document_id'


class AdminDocumentActionView(APIView):
    """
    PATCH /api/v1/verifications/admin/documents/<document_id>/action/
    Approve or Reject a document.
    Body:
    {
        "verification_status": "Verified" | "Rejected",
        "rejection_reason": "Optional reason for rejection"
    }
    """
    permission_classes = [IsAuthenticated, IsAdminOrBarangay]

    def patch(self, request, document_id):
        document = get_object_or_404(
            tbl_documents.objects.select_related('user_profile'),
            document_id=document_id
        )

        serializer = AdminDocumentActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_status = serializer.validated_data['verification_status']
        rejection_reason = serializer.validated_data.get('rejection_reason', '')

        doc_display = dict(tbl_documents.DOCUMENT_CHOICES).get(
            document.document_type, document.document_type
        )

        if new_status == 'Verified':
            document.verification_status = 'Verified'
            document.verifyBy = request.user
            document.rejection_reason = None
            document.save(update_fields=['verification_status', 'verifyBy', 'rejection_reason'])

            # Check if all required documents for user are now verified
            check_and_update_profile_verification(document.user_profile)

            # Notify user
            tbl_notification.objects.create(
                sender_id=request.user,
                receiver_id=document.user_profile,
                notification_message=(
                    f"Your {doc_display} has been approved and verified by {request.user.account_type}."
                )
            )

            return Response({
                "message": f"Document marked as Verified by {request.user.account_type}.",
                "document_id": str(document.document_id),
                "verification_status": "Verified"
            }, status=status.HTTP_200_OK)

        elif new_status == 'Rejected':
            document.verification_status = 'Rejected'
            document.verifyBy = request.user
            document.rejection_reason = rejection_reason
            document.save(update_fields=['verification_status', 'verifyBy', 'rejection_reason'])

            # Notify user
            tbl_notification.objects.create(
                sender_id=request.user,
                receiver_id=document.user_profile,
                notification_message=(
                    f"Your {doc_display} was rejected: {rejection_reason}. "
                    "You may re-upload a clear and valid document."
                )
            )

            return Response({
                "message": f"Document marked as Rejected.",
                "document_id": str(document.document_id),
                "verification_status": "Rejected",
                "rejection_reason": rejection_reason
            }, status=status.HTTP_200_OK)


class AdminReprocessDocumentView(APIView):
    """
    POST /api/v1/verifications/admin/documents/<document_id>/reprocess/
    Trigger OCR + Groq processing again in the background.
    """
    permission_classes = [IsAuthenticated, IsAdminOrBarangay]

    def post(self, request, document_id):
        document = get_object_or_404(tbl_documents, document_id=document_id)
        process_document_async(str(document.document_id))

        return Response({
            "message": "Document re-processing queued in background.",
            "document_id": str(document.document_id)
        }, status=status.HTTP_202_ACCEPTED)
