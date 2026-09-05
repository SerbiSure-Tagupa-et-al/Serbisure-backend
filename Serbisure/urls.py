from django.contrib import admin 
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

"""
URL configuration for Serbisure project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('testing_database.urls')),
    path('api/v1/accounts/', include('accounts.urls')),
    path('api/v1/verifications/', include('verifications.urls')),
    path('api/v1/booking/', include('booking.urls')),
    path('api/v1/chat/', include('chat.urls')),
    path('api/v1/reviews/', include('reviews.urls')),
    path('api/v1/notifications/', include('notifications.urls')),

    # 1. This generates the raw JSON file of your API structure
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),

    # 2. This creates the beautiful Swagger UI Webpage using that JSON!
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui')
]
