from django.urls import path
from .views import (
    NotificationListView,
    NotificationMarkReadView,
    NotificationMarkAllReadView,
)

urlpatterns = [
    path('', NotificationListView.as_view(), name='notification-list'),
    path('<uuid:pk>/read/', NotificationMarkReadView.as_view(), name='notification-mark-read'),
    path('mark-all-read/', NotificationMarkAllReadView.as_view(), name='notification-mark-all-read'),
]
