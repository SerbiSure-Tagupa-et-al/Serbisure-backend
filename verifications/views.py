from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from .models import tbl_documents
from .serializers import DocumentUploadSerializer, AdminVerificationQueueSerializer
from verifications.services.audit_logger import record_audit_log
from rest_framework.throttling import UserRateThrottle
from rest_framework.exceptions import Throttled
from rest_framework import status
from rest_framework.response import Response
import math
class DocumentUploadThrottle(UserRateThrottle):
    rate = '20/d'


class DocumentUploadView(generics.CreateAPIView):
    throttle_classes = [DocumentUploadThrottle]
    queryset = tbl_documents.objects.all()
    serializer_class = DocumentUploadSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = (MultiPartParser, FormParser)

    def create(self, request, *args, **kwargs):
        user = request.user
        doc_type = request.data.get('document_type')

        if user.account_type == 'Kasambahay' and doc_type not in ['nbi_clearance', 'police_clearance']:
            return Response(
                {"error": "Kasambahay can only upload NBI or Police Clearances"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if user.account_type == 'Homeowner' and doc_type not in ['national_id_front', 'national_id_back']:
            return Response(
                {"error": "Homeowner can only upload a National ID"},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Allow re-upload if previous document was Rejected. Block if Pending or Verified!
        active_doc = tbl_documents.objects.filter(
            user_profile=user, document_type=doc_type,
        ).order_by('-created_at', '-pk').first()

        if active_doc and active_doc.verification_status in ['Pending', 'Verified']:
            status_text = "is currently pending review" if active_doc.verification_status == "Pending" else "is already verified"
            return Response(
                {"error": f"You have already submitted your {doc_type} ({status_text})."},
                status=status.HTTP_409_CONFLICT
            )
        
        response = super().create(request, *args, **kwargs)

        # Create user notification
        from notifications.models import tbl_notification
        doc_display = dict(tbl_documents.DOCUMENT_CHOICES).get(doc_type, doc_type)
        try:
            tbl_notification.objects.create(
                sender_id=user,
                receiver_id=user,
                notification_message=f"Your {doc_display} has been submitted and is queued for verification.",
            )
        except Exception:
            pass

        return response
    
    def throttled(self, request, wait):
        if wait > 3600:
            time_left = math.ceil(wait / 3600)
            custom_message = f"Too many attempts. Please try again in {time_left} hours."
        else:
            custom_message = f"Too many attempts. Please try again in {math.ceil(wait / 60)} minutes"

        raise Throttled(detail=custom_message)


class UserVerificationStatusView(generics.GenericAPIView):
    """
    GET /api/v1/verifications/status/
    Returns the authenticated user's submitted documents, their status,
    any rejection reasons, and the overall account verification status.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .serializers_admin import UserDocumentStatusSerializer
        from .services.document_processor import document_needs_retry, process_document_async

        user = request.user
        documents = tbl_documents.objects.filter(user_profile=user).order_by('-created_at')
        for doc in documents:
            if document_needs_retry(doc):
                job = process_document_async(str(doc.pk), retry=True)
                if job is not None:
                    doc.refresh_from_db()

        serialized_docs = UserDocumentStatusSerializer(documents, many=True).data

        # Determine required document types depending on account type
        if user.account_type == 'Kasambahay':
            required_docs = ['nbi_clearance', 'police_clearance']
        elif user.account_type == 'Homeowner':
            required_docs = ['national_id_front', 'national_id_back']
        else:
            required_docs = []

        return Response({
            "account_type": user.account_type,
            "overall_status": user.verification_status,
            "required_documents": required_docs,
            "user_info": {
                "first_name": user.first_name,
                "middle_name": user.middle_name,
                "last_name": user.last_name,
                "date_of_birth": str(user.date_of_birth) if user.date_of_birth else None,
            },
            "documents": serialized_docs,
        }, status=status.HTTP_200_OK)


class UserDeleteRejectedDocumentView(generics.GenericAPIView):
    """
    DELETE /api/v1/verifications/documents/<document_id>/
    Allows user to delete a document ONLY IF it has been rejected,
    allowing them to clean up before re-submitting.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request, document_id):
        try:
            doc = tbl_documents.objects.get(document_id=document_id, user_profile=request.user)
        except tbl_documents.DoesNotExist:
            return Response({"error": "Document not found."}, status=status.HTTP_404_NOT_FOUND)

        if doc.verification_status != 'Rejected':
            return Response(
                {"error": f"Cannot delete a document with status '{doc.verification_status}'. Only rejected documents can be deleted."},
                status=status.HTTP_400_BAD_REQUEST
            )

        record_audit_log(
            document=doc,
            action='DELETED',
            actor=request.user,
            target_user=doc.user_profile,
            previous_status=doc.verification_status,
            new_status='Deleted',
            reason="User deleted rejected document prior to re-upload.",
            request=request
        )
        doc.delete()
        return Response({"message": "Rejected document removed successfully."}, status=status.HTTP_200_OK)


# Backward-compatible alias
DeleteRejectedDocumentView = UserDeleteRejectedDocumentView
DocumentStatusView = UserVerificationStatusView



# ==========================================
# ADMIN VERIFICATION QUEUE & REVIEW ENDPOINTS
# ==========================================

class AdminVerificationQueueView(generics.ListAPIView):
    """Current required documents, grouped by the same rules as portal statistics."""
    from .views_admin import IsAdminOrBarangay
    permission_classes = [IsAuthenticated, IsAdminOrBarangay]
    serializer_class = AdminVerificationQueueSerializer

    def list(self, request, *args, **kwargs):
        from .services.queue import verification_queue
        response = Response(verification_queue(request))
        response['Cache-Control'] = 'private, no-store'
        return response


class AdminVerificationReviewView(generics.GenericAPIView):
    """
    Allows Admin or Barangay officer to approve or reject a document.
    POST body:
    {
        "action": "approve" | "reject" | "reset",
        "rejection_reason": "Optional notes or reason for rejection"
    }
    """
    from .views_admin import IsAdminOrBarangay
    permission_classes = [IsAuthenticated, IsAdminOrBarangay]

    def post(self, request, document_id, *args, **kwargs):
        try:
            document = tbl_documents.objects.select_related('user_profile').get(document_id=document_id)
        except tbl_documents.DoesNotExist:
            return Response({"error": "Document not found"}, status=status.HTTP_404_NOT_FOUND)

        action = request.data.get('action', '').lower()
        reason = request.data.get('rejection_reason', '')

        if action not in ['approve', 'reject', 'reset']:
            return Response({"error": "action must be 'approve', 'reject', or 'reset'"}, status=status.HTTP_400_BAD_REQUEST)

        user = document.user_profile

        if request.user.account_type == 'Barangay' and (request.user.barangay or '').strip().casefold() != (user.barangay or '').strip().casefold():
            return Response({'error': 'This document is outside your barangay.'}, status=403)
        reviewer = request.user

        prev_status = document.verification_status

        if action == 'approve':
            document.verification_status = 'Verified'
            document.rejection_reason = None
            if reviewer:
                document.verifyBy = reviewer
            document.save()

            record_audit_log(
                document=document,
                action='APPROVED',
                actor=reviewer,
                target_user=user,
                previous_status=prev_status,
                new_status='Verified',
                reason=None,
                request=request
            )

            try:
                from notifications.models import tbl_notification
                doc_display = dict(tbl_documents.DOCUMENT_CHOICES).get(document.document_type, document.document_type)
                role_label = getattr(reviewer, 'account_type', 'Admin') if reviewer else 'Admin'
                tbl_notification.objects.create(
                    sender_id=reviewer or user,
                    receiver_id=user,
                    notification_message=f"Your {doc_display} has been approved and verified by {role_label}."
                )
            except Exception:
                pass

            return Response({
                "message": "Document approved successfully.",
                "document": AdminVerificationQueueSerializer(document).data
            }, status=status.HTTP_200_OK)

        elif action == 'reject':
            document.verification_status = 'Rejected'
            document.rejection_reason = reason or "Document criteria not met"
            if reviewer:
                document.verifyBy = reviewer
            document.save()

            record_audit_log(
                document=document,
                action='REJECTED',
                actor=reviewer,
                target_user=user,
                previous_status=prev_status,
                new_status='Rejected',
                reason=reason or "Document criteria not met",
                request=request
            )

            # NOTE: DO NOT reject companion documents!
            # If Front ID is rejected, Back ID must NOT be rejected.
            # If NBI Clearance is rejected, Police Clearance must NOT be rejected.
            # Each document can be rejected independently.


            try:
                from notifications.models import tbl_notification
                doc_display = dict(tbl_documents.DOCUMENT_CHOICES).get(document.document_type, document.document_type)
                role_label = getattr(reviewer, 'account_type', 'Admin') if reviewer else 'Admin'
                tbl_notification.objects.create(
                    sender_id=reviewer or user,
                    receiver_id=user,
                    notification_message=f"Your {doc_display} was rejected by {role_label}: {reason or 'Document criteria not met'}. You may re-upload a clear and valid document."
                )
            except Exception:
                pass

            return Response({
                "message": "Document rejected.",
                "document": AdminVerificationQueueSerializer(document).data
            }, status=status.HTTP_200_OK)

        elif action == 'reset':
            document.verification_status = 'Pending'
            document.rejection_reason = None
            document.verifyBy = None
            document.save()

            record_audit_log(
                document=document,
                action='RESET',
                actor=reviewer,
                target_user=user,
                previous_status=prev_status,
                new_status='Pending',
                reason="Document reset to Pending review",
                request=request
            )

            # NOTE: DO NOT reset companion documents!
            # Resetting Front ID must NOT reset Back ID.


            return Response({
                "message": "Document reset to Pending review.",
                "document": AdminVerificationQueueSerializer(document).data
            }, status=status.HTTP_200_OK)

