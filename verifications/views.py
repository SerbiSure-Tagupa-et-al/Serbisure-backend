from rest_framework import generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from .models import tbl_documents
from .serializers import DocumentUploadSerializer
from rest_framework.throttling import UserRateThrottle
from rest_framework.exceptions import Throttled
from core.utils import check_valid_uuid
from rest_framework import status
from rest_framework.response import Response
import math 

class DocumentUploadThrottle(UserRateThrottle):
    rate = '5/d'

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
            user_profile=user,
            document_type=doc_type,
            verification_status__in=['Pending', 'Verified']
        ).first()

        if active_doc:
            status_text = "is currently pending review" if active_doc.verification_status == "Pending" else "is already verified"
            return Response(
                {"error": f"You have already submitted your {doc_type} ({status_text})."},
                status=status.HTTP_409_CONFLICT
            )
        
        response = super().create(request, *args, **kwargs)

        # Update user's profile verification_status to 'Pending' if it was 'Unverified'
        if user.verification_status in ['Unverified', 'Rejected']:
            user.verification_status = 'Pending'
            user.save(update_fields=['verification_status'])

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
        # 3600 seconds = 1 hour
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
        user = request.user
        documents = tbl_documents.objects.filter(user_profile=user).order_by('-created_at')
        serialized_docs = UserDocumentStatusSerializer(documents, many=True).data

        # Determine required document types depending on account type
        if user.account_type == 'Kasambahay':
            required_docs = ['nbi_clearance', 'police_clearance']
            has_verified = any(d['verification_status'] == 'Verified' for d in serialized_docs)
            has_pending = any(d['verification_status'] == 'Pending' for d in serialized_docs)
            has_rejected = any(d['verification_status'] == 'Rejected' for d in serialized_docs)
        elif user.account_type == 'Homeowner':
            required_docs = ['national_id_front', 'national_id_back']
            verified_types = {d['document_type'] for d in serialized_docs if d['verification_status'] == 'Verified'}
            has_verified = ('national_id_front' in verified_types and 'national_id_back' in verified_types)
            has_pending = any(d['verification_status'] == 'Pending' for d in serialized_docs)
            has_rejected = any(d['verification_status'] == 'Rejected' for d in serialized_docs)
        else:
            required_docs = []
            has_verified = (user.verification_status == 'Verified')
            has_pending = False
            has_rejected = False

        if has_verified:
            computed_status = 'Verified'
        elif has_rejected and not has_pending:
            computed_status = 'Rejected'
        elif has_pending or serialized_docs:
            computed_status = 'Pending'
        else:
            computed_status = 'Unverified'

        return Response({
            "account_type": user.account_type,
            "overall_status": user.verification_status or computed_status,
            "required_documents": required_docs,
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

        doc.delete()
        return Response({"message": "Rejected document removed successfully."}, status=status.HTTP_200_OK)