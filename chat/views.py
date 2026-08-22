from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.response import Response
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.db.models import Q, Max, Count, Subquery, OuterRef
from core.utils import check_valid_uuid
from .models import tbl_chat_message
from .serializers import (
    SendMessageSerializer,
    ChatMessageSerializer,
    ChatInboxSerializer,
    MarkMessageReadSerializer
)
import math
import cloudinary.utils

User = get_user_model()


# ─────────────────────────────────────────────
# Throttle Classes
# ─────────────────────────────────────────────

class SendMessageThrottle(UserRateThrottle):
    scope = 'chat_send'
    rate = '60/m'


class ChatMessageThrottle(UserRateThrottle):
    scope = 'chat_messages'
    rate = '120/m'


class ChatInboxThrottle(UserRateThrottle):
    scope = 'chat_inbox'
    rate = '60/m'


class MarkMessageReadThrottle(UserRateThrottle):
    scope = 'chat_read'
    rate = '60/m'


# ─────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────

def _get_throttle_message(wait):
    """Returns a standardized throttle message based on wait time in seconds."""
    if wait > 3600:
        time_left = math.ceil(wait / 3600)
        return f"Too many requests. Please try again in {time_left} hours."
    return f"Too many requests. Please try again in {math.ceil(wait / 60)} minutes."


# ─────────────────────────────────────────────
# POST /api/v1/chat/send/
# ─────────────────────────────────────────────

class SendMessageView(generics.CreateAPIView):
    """
    Sends a new message to another user.
    Requires a valid UUIDv4 Idempotency-Key header to prevent duplicate messages on network retry.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [SendMessageThrottle]
    serializer_class = SendMessageSerializer

    def create(self, request, *args, **kwargs):

        # Step 1: Validate Idempotency-Key header
        idempotency_key = request.headers.get('Idempotency-Key')

        if not idempotency_key or not check_valid_uuid(idempotency_key):
            return Response(
                {"detail": "The Idempotency-Key header is required and must be a valid UUID v4."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Step 2: Check cache — prevent duplicate sends on network retry
        cached_response = cache.get(f'chat_send_{idempotency_key}')
        if cached_response:
            return Response(cached_response['data'], status=cached_response['status'])

        # Step 3: Validate serializer input
        serializer = self.get_serializer(data=request.data)
        if serializer.is_valid(raise_exception=True):

            # Step 4: Save with sender set to authenticated user
            serializer.save(sender_id=request.user)

            response_data = {
                "message": "Message sent successfully.",
                "data": serializer.data
            }
            response_status = status.HTTP_201_CREATED

            # Step 5: Store in cache for 1 hour (idempotency window)
            cache.set(
                f'chat_send_{idempotency_key}',
                {'data': response_data, 'status': response_status},
                timeout=3600
            )

            return Response(response_data, status=response_status)

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))


# ─────────────────────────────────────────────
# GET /api/v1/chat/thread/<uuid:partner_id>/
# ─────────────────────────────────────────────

class ChatMessageView(generics.ListAPIView):
    """
    Retrieves the paginated two-way conversation thread
    between the authenticated user and a specific partner.
    Oldest messages first (top-to-bottom reading order).
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChatMessageThrottle]
    serializer_class = ChatMessageSerializer

    def get_queryset(self):
        partner_id = self.kwargs.get('partner_id')
        current_user = self.request.user

        # Validate partner UUID
        if not partner_id or not check_valid_uuid(str(partner_id)):
            raise ValidationError({"detail": "Invalid or missing partner UUID."})

        # Authorization guard: Fetch only messages between these two users
        return tbl_chat_message.objects.filter(
            Q(sender_id=current_user, receiver_id=partner_id) |
            Q(sender_id=partner_id, receiver_id=current_user),
            is_deleted=False
        ).select_related('sender_id', 'receiver_id').order_by('createdAt')

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))


# ─────────────────────────────────────────────
# GET /api/v1/chat/inbox/
# ─────────────────────────────────────────────

class ChatInboxView(generics.GenericAPIView):
    """
    Returns a list of unique conversation partners for the authenticated user.
    Each item shows: partner info, last message preview, timestamp, and unread count.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChatInboxThrottle]
    serializer_class = ChatInboxSerializer

    def get(self, request, *args, **kwargs):
        current_user = request.user

        # Step 1: Find all unique partner IDs this user has talked to
        sent_to = tbl_chat_message.objects.filter(
            sender_id=current_user,
            is_deleted=False
        ).values_list('receiver_id', flat=True).distinct()

        received_from = tbl_chat_message.objects.filter(
            receiver_id=current_user,
            is_deleted=False
        ).values_list('sender_id', flat=True).distinct()

        # Merge both directions into a unique set of partner IDs
        partner_ids = set(list(sent_to) + list(received_from))

        inbox = []

        for partner_id in partner_ids:
            try:
                partner = User.objects.get(id=partner_id)
            except User.DoesNotExist:
                continue

            # Step 2: Get most recent message in the conversation
            last_msg = tbl_chat_message.objects.filter(
                Q(sender_id=current_user, receiver_id=partner) |
                Q(sender_id=partner, receiver_id=current_user),
                is_deleted=False
            ).order_by('-createdAt').first()

            if not last_msg:
                continue

            # Step 3: Count unread messages from this partner
            unread_count = tbl_chat_message.objects.filter(
                sender_id=partner,
                receiver_id=current_user,
                is_read=False,
                is_deleted=False
            ).count()

            # Step 4: Build Cloudinary signed URL if partner has profile image
            partner_profile_image = None
            public_id = getattr(partner, 'profile_link', None)
            if public_id:
                partner_profile_image, _ = cloudinary.utils.cloudinary_url(
                    public_id,
                    type="authenticated",
                    sign_url=True
                )

            inbox.append({
                'partner_id': partner.id,
                'partner_name': f"{partner.first_name} {partner.last_name}".strip(),
                'partner_account_type': partner.account_type,
                'partner_profile_link': public_id,
                'last_message': last_msg.message_payload,
                'last_message_time': last_msg.createdAt,
                'unread_count': unread_count,
            })

        # Step 5: Sort inbox by most recent message
        inbox.sort(key=lambda x: x['last_message_time'], reverse=True)

        serializer = self.get_serializer(inbox, many=True)
        return Response({
            "message": "Inbox retrieved successfully.",
            "data": serializer.data
        }, status=status.HTTP_200_OK)

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))


# ─────────────────────────────────────────────
# PATCH /api/v1/chat/read/<uuid:message_id>/
# ─────────────────────────────────────────────

class MarkMessageReadView(generics.UpdateAPIView):
    """
    Marks a specific message as read.
    Only the intended receiver of the message can mark it as read.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [MarkMessageReadThrottle]
    serializer_class = MarkMessageReadSerializer
    http_method_names = ['patch']

    def get_object(self):
        message_id = self.kwargs.get('message_id')

        # Validate message_id UUID
        if not message_id or not check_valid_uuid(str(message_id)):
            raise ValidationError({"detail": "Invalid or missing message UUID."})

        try:
            message = tbl_chat_message.objects.get(chat_message_id=message_id)
        except tbl_chat_message.DoesNotExist:
            raise ValidationError({"detail": "Message not found."})

        # Authorization guard: Only the receiver can mark as read
        if message.receiver_id != self.request.user:
            raise ValidationError({"detail": "You are not authorized to mark this message as read."})

        return message

    def patch(self, request, *args, **kwargs):
        message = self.get_object()

        if message.is_read:
            return Response(
                {"message": "Message is already marked as read."},
                status=status.HTTP_200_OK
            )

        message.is_read = True
        message.save(update_fields=['is_read'])

        return Response(
            {"message": "Message marked as read successfully."},
            status=status.HTTP_200_OK
        )

    def throttled(self, request, wait):
        raise Throttled(detail=_get_throttle_message(wait))
