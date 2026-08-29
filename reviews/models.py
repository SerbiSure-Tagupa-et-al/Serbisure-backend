from django.db import models
from django.conf import settings
from django.db.models import CheckConstraint, UniqueConstraint, F, Q
from django.core.validators import MinValueValidator, MaxValueValidator
import uuid

class tbl_review(models.Model):

    NLP_TYPE_CHOICES = (
        ('Positive', 'Positive'),
        ('Neutral', 'Neutral'),
        ('Negative', 'Negative')
    )

    review_id = models.UUIDField(
        primary_key=True, 
        default=uuid.uuid4, 
        editable=False
    )
    
    booking_id = models.ForeignKey(
        'booking.tbl_booking', # Points safely to the booking app
        on_delete=models.CASCADE,
        related_name='reviews',
        db_column='booking_id'
    )
    
    reviewer_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reviews_given',
        db_column='reviewer_id'
    )
    
    reviewee_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reviews_received',
        db_column='reviewee_id'
    )
    
    unstructured_feedback = models.TextField(
        max_length=1000,
        blank=False, 
        null=False
    )
        
    nlp_sentiment = models.CharField(
        max_length=20, 
        blank=False, 
        null=False,
        choices=NLP_TYPE_CHOICES
    )

    rating = models.IntegerField(
        validators=[
            MinValueValidator(1),
            MaxValueValidator(5)
        ],
        blank=False,
        null=False
    )

    createdAt = models.DateTimeField(
        auto_now_add=True
    )
    class Meta:
        db_table = 'tbl_review'
        constraints = [
            CheckConstraint(
                condition=Q(nlp_sentiment__in=['Positive', 'Negative', 'Neutral']),
                name='valid_nlp_sentiment_enum'
            ),

            UniqueConstraint(
                fields=['booking_id', 'reviewer_id'],
                name='one_review_per_user_per_booking'
            ),

            CheckConstraint(
                condition=Q(rating__gte=1, rating__lte=5),
                name='valid_rating_range_1_to_5'
            ),

            CheckConstraint(
                condition=~Q(reviewer_id=F('reviewee_id')),
                name='reviewer_cannot_be_reviewee'
            ),
        ]
