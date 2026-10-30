from django.utils import timezone
from rest_framework import serializers

from accounts.workforce import PH_TIME


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({'non_field_errors': ['Expected an object.']})
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({key: 'This field cannot be edited here.' for key in unknown})
        return super().to_internal_value(data)


class ProfileCorrectionSerializer(StrictSerializer):
    first_name = serializers.CharField(max_length=100, required=False)
    middle_name = serializers.CharField(max_length=100, allow_blank=True, allow_null=True, required=False)
    last_name = serializers.CharField(max_length=100, required=False)
    date_of_birth = serializers.DateField(allow_null=True, required=False)

    def validate_date_of_birth(self, value):
        if value and value > timezone.localdate(timezone=PH_TIME):
            raise serializers.ValidationError('Birthdate cannot be in the future.')
        return value


class DocumentCorrectionSerializer(ProfileCorrectionSerializer):
    first_name = serializers.CharField(max_length=100, allow_null=True, allow_blank=True, required=False)
    last_name = serializers.CharField(max_length=100, allow_null=True, allow_blank=True, required=False)
    document_number = serializers.CharField(max_length=100, allow_null=True, allow_blank=True, required=False)
    date_issued = serializers.DateField(allow_null=True, required=False)
    valid_until = serializers.DateField(allow_null=True, required=False)

    def validate_date_issued(self, value):
        if value and value > timezone.localdate(timezone=PH_TIME):
            raise serializers.ValidationError('Issue date cannot be in the future. Check the year on the document.')
        return value


class ReviewerCorrectionSerializer(StrictSerializer):
    version = serializers.RegexField(r'^[a-f0-9]{64}$')
    profile = ProfileCorrectionSerializer(required=False)
    document = DocumentCorrectionSerializer(required=False)

    def validate(self, data):
        if not data.get('profile') and not data.get('document'):
            raise serializers.ValidationError('Choose at least one field to correct.')
        return data
