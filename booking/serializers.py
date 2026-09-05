from rest_framework import serializers
from .models import tbl_booking
from django.utils import timezone
import cloudinary.utils

class BookingSerializer(serializers.ModelSerializer):

    class Meta:

        model = tbl_booking
        fields = ['booking_id', 'booking_type', 'booking_status', 'service_category', 'start_time', 'end_time', 'service_address', 'floor_number', 'zip_code', 'special_instruction', 'daily_rate', 'poster_id', 'createdAt']
        read_only_fields = ['booking_id', 'booking_status', 'poster_id', 'createdAt']

    def validate(self, data):

        now = timezone.now()
        start_time = data.get('start_time')
        end_time = data.get('end_time')

        if start_time and start_time <= now: 
            raise serializers.ValidationError({"start_time": "Start time must be in the future."})
        
        if end_time and  end_time <= now:
            raise serializers.ValidationError({"end_time": "End time must be in the future."})

        if (start_time and end_time) and end_time <= start_time:
            raise serializers.ValidationError({"end_time": "End time must be strictly after the start time."})

        return data

class BookingFeedSerializer(serializers.ModelSerializer):

    profile_link = serializers.SerializerMethodField()
    name = serializers.SerializerMethodField()
    poster_account_type = serializers.SerializerMethodField()

    class Meta:
        model = tbl_booking

        fields = [
            'booking_id',
            'poster_id',
            'poster_account_type',
            'booking_type',
            'profile_link',
            'name',
            'service_address',
            'service_category',
            'daily_rate'
        ]

    def get_poster_account_type(self, obj):
        return getattr(obj.poster_id, 'account_type', 'User')

    
    def get_name(self, obj):
        
        first_name = obj.poster_id.first_name or ''
        middle_name = obj.poster_id.middle_name or ''
        last_name = obj.poster_id.last_name or ''

        return f"{first_name} {middle_name} {last_name}".strip()
    
    def get_profile_link(self, obj):

        public_id = obj.poster_id.profile_link
        
        if not public_id: 
            return None
        
        temporary_url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            type="authenticated",
            sign_url=True
        )

        return temporary_url