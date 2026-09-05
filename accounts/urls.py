from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    UserRegistrationView,
    CustomLoginView,
    ProfileImageUploadView,
    UserAboutView,
    UserTagsView,
    PublicProfileView,
    KasambahayResumeView,
    ChangePasswordView,
)


urlpatterns = [

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

    # Public Profile Endpoint
    path('public-profile/<uuid:id>/', PublicProfileView.as_view(), name='public-profile'),

    # Kasambahay Resume Endpoint
    path('resume/', KasambahayResumeView.as_view(), name='kasambahay-resume'),

    # Change Password Endpoint
    path('change-password/', ChangePasswordView.as_view(), name='change-password'),

    # Refresh Endpoints (Used when the access token expires to get a new one)
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh')
]