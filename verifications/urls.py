from django.urls import path
from .views import (
    DocumentUploadView,
    UserVerificationStatusView,
    UserDeleteRejectedDocumentView,
)
from .views_admin import (
    AdminDocumentListView,
    AdminDocumentDetailView,
    AdminDocumentActionView,
    AdminReprocessDocumentView,
)

urlpatterns = [
    # User-facing endpoints
    path('upload/', DocumentUploadView.as_view(), name='document-upload'),
    path('status/', UserVerificationStatusView.as_view(), name='document-status'),
    path('documents/<uuid:document_id>/', UserDeleteRejectedDocumentView.as_view(), name='document-delete-rejected'),

    # Admin / Barangay review endpoints
    path('admin/documents/', AdminDocumentListView.as_view(), name='admin-document-list'),
    path('admin/documents/<uuid:document_id>/', AdminDocumentDetailView.as_view(), name='admin-document-detail'),
    path('admin/documents/<uuid:document_id>/action/', AdminDocumentActionView.as_view(), name='admin-document-action'),
    path('admin/documents/<uuid:document_id>/reprocess/', AdminReprocessDocumentView.as_view(), name='admin-document-reprocess'),
]