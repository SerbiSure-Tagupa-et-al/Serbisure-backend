from rest_framework import serializers
from .models import tbl_chat_message
from django.contrib.auth import get_user_model
import cloudinary.utils

User = get_user_model()


class SendMessageSerializer(serializers.ModelSerializer):
    """
    Serializer for validating and creating new chat messages.
    """
    message_payload = serializers.CharField(
        max_length=500,
        required=True,
        allow_blank=False,
        error_messages={
            "blank": "Message content cannot be empty.",
            "max_length": "Message cannot exceed 500 characters."
        }
    )

    class Meta:
        model = tbl_chat_message
        fields = [
            'chat_message_id',
            'sender_id',
            'receiver_id',
            'booking_id',
            'message_payload',
            'is_read',
            'createdAt'
        ]
        read_only_fields = ['chat_message_id', 'sender_id', 'is_read', 'createdAt']

    def validate_message_payload(self, value):
        stripped = value.strip()
        if not stripped:
            raise serializers.ValidationError("Message content cannot be blank or whitespace only.")
        return stripped

    def validate(self, attrs):
        request = self.context.get('request')
        receiver = attrs.get('receiver_id')

        # 🚫 Defense-in-depth: Prevent sending message to yourself
        if request and receiver and request.user == receiver:
            raise serializers.ValidationError({"receiver_id": "You cannot send a message to yourself."})

        return attrs


class ChatMessageSerializer(serializers.ModelSerializer):
    """
    Serializer for individual messages in a conversation thread.
    Adds `is_sender` boolean so the frontend easily positions chat bubbles left/right.
    """
    is_sender = serializers.SerializerMethodField()

    class Meta:
        model = tbl_chat_message
        fields = [
            'chat_message_id',
            'sender_id',
            'receiver_id',
            'booking_id',
            'message_payload',
            'is_read',
            'is_sender',
            'createdAt'
        ]

    def get_is_sender(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return obj.sender_id_id == request.user.id
        return False


class ChatInboxSerializer(serializers.Serializer):
    """
    Serializer for the conversation list (inbox).
    """
    partner_id = serializers.UUIDField()
    partner_name = serializers.CharField()
    partner_account_type = serializers.CharField()
    partner_profile_image = serializers.SerializerMethodField()
    last_message = serializers.CharField()
    last_message_time = serializers.DateTimeField()
    unread_count = serializers.IntegerField(default=0)

    def get_partner_profile_image(self, obj):
        public_id = obj.get('partner_profile_link')
        if not public_id:
            return None

        temporary_url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            type="authenticated",
            sign_url=True
        )
        return temporary_url


class MarkMessageReadSerializer(serializers.ModelSerializer):
    """
    Serializer for marking a message as read.
    """
    class Meta:
        model = tbl_chat_message
        fields = ['chat_message_id', 'is_read']
        read_only_fields = ['chat_message_id']
