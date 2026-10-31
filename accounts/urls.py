from django.urls import path
from .penalty_api import PenaltyUsersView, PenaltyReviewView, PenaltyHistoryView, PenaltyExportView
from .worker_views import WorkerProfileView, WorkerCvView, WorkerCvExportView
from .token_views import AccountTokenRefreshView as TokenRefreshView
from .views import (
    UserRegistrationView,
    CustomLoginView,
    ProfileImageUploadView,
    UserAboutView,
    UserTagsView,
    ContactPrivacyView,
    UserSocialLinksView,
    JobStatusView,
    PublicProfileView,
    ChangePasswordView,
    UserSearchView,
    DeleteAccountView,
    ExportUserDataView,
    AdminUserListView,
    AdminDashboardStatsView,
    AdminDashboardActivityView,
    AdminMonthlyTrendView,
    AdminLoginView,
    AdminActiveBarangaysView,
    AdminVerificationStatusStatsView,
    AdminBarangayDeskProfileView,
)

urlpatterns = [
    path('admin/penalties/users/', PenaltyUsersView.as_view(), name='penalty-admin-users'),
    path('admin/penalties/users/<uuid:user_id>/', PenaltyReviewView.as_view(), name='penalty-admin-review'),
    path('admin/penalties/history/', PenaltyHistoryView.as_view(), name='penalty-admin-history'),
    path('admin/penalties/export/', PenaltyExportView.as_view(), name='penalty-admin-export'),
    path('worker-profile/', WorkerProfileView.as_view(), name='worker-profile'),
    path('worker-cv/<uuid:user_id>/', WorkerCvView.as_view(), name='worker-cv'),
    path('worker-cv/<uuid:user_id>/export/', WorkerCvExportView.as_view(), name='worker-cv-export'),
    # Registration Endpoints
    path('register/', UserRegistrationView.as_view(), name='register'),
    
    # Login Endpoints
    path('login/', CustomLoginView.as_view(), name='login'),

    # Profile Endpoints
    path('profile-image/', ProfileImageUploadView.as_view(), name='profile-image'),

    # User About Endpoints 
    path('user-about/', UserAboutView.as_view(), name='user-about'),

    # User Tags Endpoints
    path('user-tags/', UserTagsView.as_view(), name='user-tags'),

    # Contact Privacy Endpoint
    path('contact-privacy/', ContactPrivacyView.as_view(), name='contact-privacy'),

    # Social Links Endpoint
    path('social-links/', UserSocialLinksView.as_view(), name='user-social-links'),

    # Kasambahay Availability / Job Status Endpoint
    path('job-status/', JobStatusView.as_view(), name='job-status'),

    # Public Profile Endpoint
    path('public-profile/<uuid:id>/', PublicProfileView.as_view(), name='public-profile'),

    # User Search Endpoint (Tier 2-2)
    path('search/', UserSearchView.as_view(), name='user-search'),

    # Delete Account Endpoint (Tier 3-5)
    path('delete-account/', DeleteAccountView.as_view(), name='delete-account'),

    # Export User Data Endpoint (Tier 3-5)
    path('export-data/', ExportUserDataView.as_view(), name='export-data'),

    # Change Password Endpoint
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),

    # Refresh Endpoints (Used when the access token expires to get a new one)
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # Admin Endpoints
    path('admin/login/', AdminLoginView.as_view(), name='admin-login'),
    path('admin/active-barangays/', AdminActiveBarangaysView.as_view(), name='admin-active-barangays'),
    path('admin/users/', AdminUserListView.as_view(), name='admin-users'),
    path('admin/dashboard-stats/', AdminDashboardStatsView.as_view(), name='admin-dashboard-stats'),
    path('admin/dashboard-activity/', AdminDashboardActivityView.as_view(), name='admin-dashboard-activity'),
    path('admin/monthly-trend/', AdminMonthlyTrendView.as_view(), name='admin-monthly-trend'),
    path('admin/verification-status-stats/', AdminVerificationStatusStatsView.as_view(), name='admin-verification-status-stats'),
    path('admin/desk-profile/', AdminBarangayDeskProfileView.as_view(), name='admin-barangay-desk-profile'),
]
