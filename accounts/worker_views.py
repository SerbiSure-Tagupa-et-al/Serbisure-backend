from django.http import HttpResponse
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from .models import tbl_user_profile, tbl_worker_profile
from .permissions import IsKasambahay
from .worker_serializers import WorkerProfileSerializer
from .cv_service import snapshot, pdf_bytes, csv_bytes, verified_work_history


class CvThrottle(UserRateThrottle):
    scope = 'worker_cv'
    rate = '30/h'


class WorkerProfileView(APIView):
    permission_classes = [IsAuthenticated, IsKasambahay]

    def get(self, request):
        instance = tbl_worker_profile.objects.filter(user=request.user).first()
        response = Response({**WorkerProfileSerializer(instance or tbl_worker_profile(user=request.user)).data,
                             'languages': [value.strip() for value in (request.user.language or '').split(',') if value.strip()]})
        response['Cache-Control'] = 'private, no-store'
        return response

    def patch(self, request):
        with transaction.atomic():
            user = tbl_user_profile.objects.select_for_update().get(pk=request.user.pk)
            instance, _ = tbl_worker_profile.objects.get_or_create(user=user)
            serializer = WorkerProfileSerializer(instance, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            instance = serializer.save()
            labels = {'Child_care': 'Child Care'}
            tags = user.user_tags or []
            fields = []
            if 'service_categories' in request.data:
                category_tags = {'Cleaning', 'Cooking', 'Cook', 'Laundry', 'Child Care', 'Childcare', 'Child_care', 'Caregiver', 'All-around'}
                tags = [labels.get(value, value) for value in instance.service_categories] + [tag for tag in tags if tag not in category_tags]
            if 'work_setup' in request.data:
                tags = [tag for tag in tags if tag not in {'Stay-In', 'Stay-Out', 'Either'}]
                setup = {'stay_in': 'Stay-In', 'stay_out': 'Stay-Out', 'either': 'Either'}.get(instance.work_setup)
                if setup:
                    tags.append(setup)
            if 'service_categories' in request.data or 'work_setup' in request.data:
                user.user_tags = tags[:10]
                fields.append('user_tags')
            if 'languages' in request.data:
                # Language choices are validated separately; all profile writes stay atomic.
                from rest_framework import serializers
                values = serializers.ListField(child=serializers.ChoiceField(choices=['Bisaya', 'Tagalog', 'English']), max_length=3).run_validation(request.data['languages'])
                user.language = ', '.join(dict.fromkeys(values))
                fields.append('language')
            if fields:
                user.save(update_fields=fields)
        return Response({**WorkerProfileSerializer(instance).data,
                         'languages': [value.strip() for value in (user.language or '').split(',') if value.strip()]})


class PublicWorkerHistoryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, user_id):
        worker = get_object_or_404(tbl_user_profile, pk=user_id, is_active=True)
        if worker.account_type not in ('Kasambahay', 'Homeowner'):
            data = {
                'user_id': str(worker.pk),
                'account_type': worker.account_type,
                'summary': {
                    'completed_jobs_count': 0,
                    'recommended_count': 0,
                    'repeat_employers_count': 0,
                },
                'jobs': [],
            }
        else:
            data = verified_work_history(worker, limit=20)
        response = Response(data)
        response['Cache-Control'] = 'private, no-store'
        return response


class WorkerCvView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [CvThrottle]

    def get(self, request, user_id):
        worker = get_object_or_404(tbl_user_profile, pk=user_id, account_type='Kasambahay', is_active=True)
        response = Response(snapshot(worker))
        response['Cache-Control'] = 'private, no-store'
        return response


class WorkerCvExportView(WorkerCvView):
    def get(self, request, user_id):
        worker = get_object_or_404(tbl_user_profile, pk=user_id, account_type='Kasambahay', is_active=True)
        file_format = request.query_params.get('file_format', 'pdf')
        if file_format not in ('pdf', 'csv'):
            return Response({'detail': 'Choose PDF or CSV.'}, status=400)
        if file_format == 'csv' and worker.pk != request.user.pk:
            return Response({'detail': 'Only the worker can export their CSV.'}, status=403)
        data = snapshot(worker)
        content = pdf_bytes(data) if file_format == 'pdf' else csv_bytes(data)
        mime = 'application/pdf' if file_format == 'pdf' else 'text/csv; charset=utf-8'
        response = HttpResponse(content, content_type=mime)
        response['Content-Disposition'] = f'attachment; filename="SerbiSure_CV.{file_format}"'
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
