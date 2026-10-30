import logging
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, BasePermission
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.db.models import Q
from .models import tbl_documents, tbl_audit_logs
from .serializers_admin import (
    AdminDocumentDetailSerializer,
    AdminDocumentActionSerializer,
    AdminAuditLogSerializer,
)
from verifications.services.document_processor import process_document_async
from verifications.services.audit_logger import record_audit_log
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

        prev_status = document.verification_status

        if new_status == 'Verified':
            document.verification_status = 'Verified'
            document.verifyBy = request.user
            document.rejection_reason = None
            document.save(update_fields=['verification_status', 'verifyBy', 'rejection_reason'])

            record_audit_log(
                document=document,
                action='APPROVED',
                actor=request.user,
                target_user=document.user_profile,
                previous_status=prev_status,
                new_status='Verified',
                reason=None,
                request=request
            )

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

            record_audit_log(
                document=document,
                action='REJECTED',
                actor=request.user,
                target_user=document.user_profile,
                previous_status=prev_status,
                new_status='Rejected',
                reason=rejection_reason,
                request=request
            )

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
                "message": "Document marked as Rejected.",
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
        record_audit_log(
            document=document,
            action='REPROCESSED',
            actor=request.user,
            target_user=document.user_profile,
            previous_status=document.verification_status,
            new_status=document.verification_status,
            reason="Triggered manual OCR + AI re-processing",
            request=request
        )
        process_document_async(str(document.document_id), force=True)

        return Response({
            "message": "Document re-processing queued in background.",
            "document_id": str(document.document_id)
        }, status=status.HTTP_202_ACCEPTED)


class AdminAuditLogListView(generics.ListAPIView):
    """
    GET /api/v1/verifications/admin/audit-logs/
    Returns immutable audit trail logs for Superadmin dashboard with search, filtering, and pagination.
    """
    from accounts.permissions import IsPortalAdmin
    permission_classes = [IsAuthenticated, IsPortalAdmin]
    serializer_class = AdminAuditLogSerializer

    def get_queryset(self):
        qs = tbl_audit_logs.objects.all().order_by('-created_at')

        from accounts.admin_statistics import barangay_name
        if self.request.user.account_type == 'Barangay':
            from rest_framework.exceptions import PermissionDenied
            own = barangay_name(self.request.user.barangay).casefold()
            if not own:
                raise PermissionDenied('Your account has no assigned barangay.')
            allowed = [pk for pk, barangay in qs.values_list('pk', 'target_barangay')
                       if barangay_name(barangay).casefold() == own]
            qs = qs.filter(pk__in=allowed)

        action_param = self.request.query_params.get('action')
        if action_param and action_param.upper() != 'ALL':
            qs = qs.filter(action=action_param.upper())

        role_param = self.request.query_params.get('role')
        if role_param and role_param.upper() != 'ALL':
            qs = qs.filter(actor_role__iexact=role_param)

        barangay_param = self.request.query_params.get('barangay')
        if self.request.user.account_type != 'Barangay' and barangay_param and barangay_param != 'All Barangays':
            scope = barangay_name(barangay_param).casefold()
            qs = qs.filter(pk__in=[pk for pk, barangay in qs.values_list('pk', 'target_barangay')
                                   if barangay_name(barangay).casefold() == scope])

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(actor_name__icontains=search) |
                Q(target_name__icontains=search) |
                Q(actor_email__icontains=search) |
                Q(target_email__icontains=search) |
                Q(document_type__icontains=search) |
                Q(reason__icontains=search)
            )

        return qs
