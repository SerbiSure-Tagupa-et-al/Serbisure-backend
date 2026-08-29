from django.contrib import admin
from .models import tbl_review


@admin.register(tbl_review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = (
        'review_id',
        'booking_id',
        'reviewer_id',
        'reviewee_id',
        'rating',
        'nlp_sentiment',
        'createdAt'
    )
    list_filter = ('rating', 'nlp_sentiment', 'createdAt')
    search_fields = (
        'reviewer_id__email',
        'reviewer_id__first_name',
        'reviewer_id__last_name',
        'reviewee_id__email',
        'reviewee_id__first_name',
        'reviewee_id__last_name',
        'unstructured_feedback'
    )
    readonly_fields = ('review_id', 'createdAt')
