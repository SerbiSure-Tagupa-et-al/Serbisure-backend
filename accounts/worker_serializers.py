from rest_framework import serializers
from decimal import Decimal
from .models import tbl_worker_profile
from booking.wage_policy import maximum_rate_for

SERVICES = ['Cleaning', 'Child_care', 'Cooking', 'Caregiver', 'Laundry', 'All-around']
SKILLS = ['Meal preparation', 'Ironing', 'Deep cleaning', 'Infant care', 'Child supervision',
          'Older adult assistance', 'Laundry care', 'Household organization']


class TrainingSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    provider = serializers.CharField(max_length=100, allow_blank=True, default='')
    year = serializers.IntegerField(min_value=1900, max_value=2100, allow_null=True, default=None)


class WorkerProfileSerializer(serializers.ModelSerializer):
    service_categories = serializers.ListField(child=serializers.ChoiceField(choices=SERVICES), max_length=6, required=False)
    practical_skills = serializers.ListField(child=serializers.ChoiceField(choices=SKILLS), max_length=8, required=False)
    available_days = serializers.ListField(child=serializers.ChoiceField(choices=['M', 'T', 'W', 'Th', 'F', 'S', 'Su']), max_length=7, required=False)
    preferred_shifts = serializers.ListField(child=serializers.ChoiceField(choices=['morning', 'afternoon', 'night']), max_length=3, required=False)
    preferred_work_areas = serializers.ListField(child=serializers.CharField(max_length=100), max_length=5, required=False)
    training = TrainingSerializer(many=True, required=False)
    number_of_children = serializers.IntegerField(min_value=0, max_value=99, allow_null=True, required=False)
    desired_monthly_rate = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('.01'), allow_null=True, required=False)
    desired_daily_rate = serializers.DecimalField(max_digits=8, decimal_places=2, min_value=Decimal('.01'), allow_null=True, required=False)

    class Meta:
        model = tbl_worker_profile
        exclude = ['id', 'user']
        read_only_fields = ['updated_at']

    def validate_service_categories(self, values):
        values = list(dict.fromkeys(values))
        return ['All-around'] if 'All-around' in values or all(value in values for value in SERVICES[:5]) else values

    def validate_training(self, values):
        if len(values) > 3:
            raise serializers.ValidationError('Add up to three training records.')
        return values

    def validate_desired_daily_rate(self, value):
        if value is not None and value > maximum_rate_for('short_term'):
            raise serializers.ValidationError(f"Maximum expected Part-time / Daily rate is ₱{maximum_rate_for('short_term'):,.2f} / day.")
        return value

    def validate_desired_monthly_rate(self, value):
        if value is not None and value > maximum_rate_for('long_term'):
            raise serializers.ValidationError(f"Maximum expected Full-time / Monthly rate is ₱{maximum_rate_for('long_term'):,.2f} / month.")
        return value

    def validate(self, attrs):
        def value(key):
            return attrs.get(key, getattr(self.instance, key, None))
        if value('experience_level') == 'new':
            attrs['experience_duration'] = ''
        if value('share_family_details') and (not value('civil_status') or value('number_of_children') is None):
            raise serializers.ValidationError({'share_family_details': 'Enter civil status and number of children before sharing these details.'})
        for key in ('practical_skills', 'available_days', 'preferred_shifts', 'preferred_work_areas'):
            if key in attrs:
                attrs[key] = list(dict.fromkeys(attrs[key]))
        return attrs
