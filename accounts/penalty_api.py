from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from booking.models import tbl_booking
from .models import tbl_user_profile
from .penalties import current_penalty_month, next_penalty_month
from .penalty_models import PenaltyAdjustment
from .penalty_report import penalty_pdf
from .penalty_management import (
    adjust_points, adjustment_history, cancellations_for, effective_points, resident_queryset,
)


class PenaltyQuery(serializers.Serializer):
    search = serializers.CharField(required=False, allow_blank=True, max_length=200)
    barangay = serializers.CharField(required=False, allow_blank=True, max_length=255)
    role = serializers.ChoiceField(choices=['Homeowner', 'Kasambahay'], required=False)
    points = serializers.ChoiceField(choices=['with_penalties', '0', '1', '2', '3'], required=False)
    user_id = serializers.UUIDField(required=False)
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(required=False, min_value=1, max_value=100)


class AdjustmentInput(serializers.Serializer):
    request_id = serializers.UUIDField()
    points_delta = serializers.IntegerField(min_value=-3, max_value=3)
    expected_points = serializers.IntegerField(min_value=0, max_value=3)
    expected_month = serializers.DateField()
    reason = serializers.CharField(min_length=10, max_length=2000)
    report_verified = serializers.BooleanField(default=False)
    incident_reference = serializers.CharField(required=False, allow_blank=True, default='', max_length=100)
    booking_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs['points_delta'] == 0:
            raise serializers.ValidationError('Choose 1–3 points to add or deduct.')
        if attrs['report_verified'] and not attrs['incident_reference']:
            raise serializers.ValidationError('Enter the verified report or blotter reference.')
        return attrs


def validated(serializer):
    if not serializer.is_valid():
        first = next(iter(serializer.errors.values()))
        raise ValidationError({'detail': str(first[0] if isinstance(first, list) else first)})
    return serializer.validated_data


class PenaltyUserSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    penalty_points = serializers.SerializerMethodField()
    is_restricted = serializers.SerializerMethodField()
    penalty_month = serializers.SerializerMethodField()
    resets_at = serializers.SerializerMethodField()

    class Meta:
        model = tbl_user_profile
        fields = ['id', 'name', 'account_type', 'barangay', 'penalty_points', 'is_restricted', 'penalty_month', 'resets_at']

    def get_name(self, obj):
        return obj.get_full_name().strip() or obj.username

    def get_penalty_points(self, obj):
        return effective_points(obj)

    def get_is_restricted(self, obj):
        return obj.penalty_month >= current_penalty_month() and (obj.is_restricted or obj.penalty_points >= 3)

    def get_penalty_month(self, obj):
        return current_penalty_month().isoformat()

    def get_resets_at(self, obj):
        return next_penalty_month().isoformat()


class PenaltyHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PenaltyAdjustment
        fields = ['id', 'user_id_snapshot', 'user_name', 'user_role', 'user_barangay',
            'admin_id_snapshot', 'admin_name', 'admin_role', 'points_delta', 'points_before', 'points_after',
            'penalty_month', 'reason', 'report_verified', 'incident_reference', 'booking_id_snapshot', 'created_at']
        read_only_fields = fields


class CancellationSerializer(serializers.ModelSerializer):
    class Meta:
        model = tbl_booking
        fields = ['booking_id', 'service_category', 'start_time', 'cancelled_at',
                  'cancellation_reason', 'cancellation_penalty_points']
        read_only_fields = fields


class PenaltyThrottle(UserRateThrottle):
    scope = 'penalty_management'
    rate = '600/hour'


class PenaltyPagination(PageNumberPagination):
    page_size = 5
    page_size_query_param = 'page_size'


class PenaltyView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [PenaltyThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response['Cache-Control'] = 'private, no-store'
        return response


class PenaltyUsersView(PenaltyView):
    def get(self, request):
        query = validated(PenaltyQuery(data=request.query_params))
        users = resident_queryset(request.user, query)
        pagination = PenaltyPagination()
        page = pagination.paginate_queryset(users, request, view=self)
        return pagination.get_paginated_response(PenaltyUserSerializer(page, many=True).data)


class PenaltyReviewView(PenaltyView):
    def get(self, request, user_id):
        target = get_object_or_404(resident_queryset(request.user), pk=user_id)
        pagination = PenaltyPagination()
        page = pagination.paginate_queryset(cancellations_for(target), request, view=self)
        return Response({'user': PenaltyUserSerializer(target).data,
            'recent_adjustments': PenaltyHistorySerializer(adjustment_history(request.user, {'user_id': user_id})[:5], many=True).data,
            'cancellations': {'count': pagination.page.paginator.count,
                'results': CancellationSerializer(page, many=True).data}})

    def post(self, request, user_id):
        data = validated(AdjustmentInput(data=request.data))
        user, record, created = adjust_points(request.user, user_id, data)
        return Response({'user': PenaltyUserSerializer(user).data,
            'record': PenaltyHistorySerializer(record).data}, status=201 if created else 200)


class PenaltyHistoryView(PenaltyView):
    def get(self, request):
        query = validated(PenaltyQuery(data=request.query_params))
        records = adjustment_history(request.user, query)
        pagination = PenaltyPagination()
        page = pagination.paginate_queryset(records, request, view=self)
        return pagination.get_paginated_response(PenaltyHistorySerializer(page, many=True).data)


class PenaltyExportView(PenaltyView):
    def get(self, request):
        query = validated(PenaltyQuery(data=request.query_params))
        records = adjustment_history(request.user, query)

        content = penalty_pdf(records.iterator(chunk_size=500), actor=request.user, filters=query, count=records.count())
        response = HttpResponse(content, content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename="penalty-adjustments.pdf"'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
