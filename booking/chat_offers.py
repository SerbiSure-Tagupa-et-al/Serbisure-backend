"""Persist a bilateral offer and its chat reference in one transaction."""
import hashlib
import json
from uuid import UUID
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.throttling import UserRateThrottle
from django.contrib.auth import get_user_model
from chat.moderation import LANGUAGE_NOTICE
from .models import tbl_booking
from .serializers import BookingSerializer, BookingDetailSerializer, ChatOfferContextSerializer
from .services import lock_participants, ensure_eligible_pair, enforce_pair_cap


class ChatOfferThrottle(UserRateThrottle):
    scope = 'chat_offer'
    rate = '50/d'


class ChatBookingOfferView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChatOfferThrottle]

    @transaction.atomic
    def post(self, request):
        try:
            key = UUID(request.headers.get('Idempotency-Key', ''))
        except (ValueError, TypeError, AttributeError):
            raise ValidationError({'detail': 'The offer request key is invalid. Reopen the offer and try again.'})
        context = ChatOfferContextSerializer(data=request.data)
        context.is_valid(raise_exception=True)
        recipient_id = context.validated_data['recipient_id']
        recipient = get_object_or_404(get_user_model(), pk=recipient_id)
        people = lock_participants(request.user.pk, recipient.pk)
        sender, recipient = people[request.user.pk], people[recipient.pk]
        from chat.moderation import inspect_outgoing
        original_instruction = str(request.data.get('special_instruction') or '')
        instruction = inspect_outgoing(sender, recipient, original_instruction, channel='booking_offer')
        ensure_eligible_pair(sender, recipient)
        digest = hashlib.sha256(json.dumps(request.data, sort_keys=True, default=str).encode()).hexdigest()
        from chat.models import tbl_chat_message
        from chat.serializers import ChatMessageSerializer
        existing = tbl_booking.objects.filter(poster_id=sender, offer_request_key=key).first()
        if existing:
            if existing.offer_payload_hash != digest:
                raise ValidationError({'detail': 'This request key has already been used for different offer terms.'})
            message = existing.chat_messages.get(is_booking_offer=True, booking_proposal__isnull=True)
            return Response({'booking': BookingDetailSerializer(existing, context={'request': request}).data,
                             'chat_message': ChatMessageSerializer(message, context={'request': request}).data,
                             'language_warning': LANGUAGE_NOTICE if instruction != original_instruction else None})
        serializer = BookingSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        required_terms = ['work_days', 'daily_start_time', 'daily_end_time', 'region', 'street']
        if serializer.validated_data['booking_type'] == 'short_term':
            required_terms.append('end_time')
        for field in required_terms:
            if not serializer.validated_data.get(field):
                raise ValidationError({field: 'Specify this agreed term before sending an offer.'})
        booking = serializer.save(poster_id=sender, recipient_id=recipient, special_instruction=instruction,
                                  offer_request_key=key, offer_payload_hash=digest)
        enforce_pair_cap(booking, recipient)
        message = tbl_chat_message.objects.create(sender_id=sender, receiver_id=recipient,
            booking_id=booking, is_booking_offer=True, message_payload='Booking offer: review the agreed terms before accepting.')
        return Response({'booking': BookingDetailSerializer(booking, context={'request': request}).data,
                         'chat_message': ChatMessageSerializer(message, context={'request': request}).data,
                         'language_warning': LANGUAGE_NOTICE if instruction != original_instruction else None}, status=201)
