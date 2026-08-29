from django.urls import path
from .views import (
    CreateReviewView,
    ReceivedReviewsView,
    GivenReviewsView,
    ReviewSummaryView,
    UserReviewsView
)

urlpatterns = [
    path('create/', CreateReviewView.as_view(), name='review-create'),
    path('received/', ReceivedReviewsView.as_view(), name='review-received'),
    path('given/', GivenReviewsView.as_view(), name='review-given'),
    path('summary/<uuid:user_id>/', ReviewSummaryView.as_view(), name='review-summary'),
    path('user/<uuid:user_id>/', UserReviewsView.as_view(), name='review-user-list'),
]
