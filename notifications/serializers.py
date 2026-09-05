from rest_framework import serializers
from .models import tbl_notification

class NotificationSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField()

    class Meta:
        model = tbl_notification
        fields = [
            'notification_id',
            'notification_message',
            'notification_state',
            'createdAt',
            'sender_id',
            'sender_name',
        ]

    def get_sender_name(self, obj):
        if obj.sender_id:
            first = getattr(obj.sender_id, 'first_name', '') or ''
            last = getattr(obj.sender_id, 'last_name', '') or ''
            name = f"{first} {last}".strip()
            return name if name else 'Serbisure'
        return 'Serbisure'
