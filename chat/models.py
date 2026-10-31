from django.db import models
from django.conf import settings
from cryptography.fernet import Fernet, InvalidToken
from django.db.models import CheckConstraint, Q, F
from django.db.models.expressions import RawSQL
import base64
import hashlib
import uuid
from django.core.exceptions import ValidationError


ACCESS_ACTIONS = (
    ('pause_chat', 'Messaging paused'),
    ('resume_chat', 'Messaging resumed'),
    ('suspend_account', 'Account suspended'),
    ('restore_account', 'Account restored'),
)


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Account access history cannot be edited.')

    def delete(self):
        raise ValidationError('Account access history cannot be deleted.')

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError('Account access history cannot be edited.')


class AccountAccessAudit(models.Model):
    """Identity and jurisdiction snapshots survive profile edits and deletion.

    Deliberately no foreign keys: account removal must never rewrite history.
    Migration-installed database triggers also reject SQL UPDATE/DELETE/TRUNCATE.
    """
    log_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_id_snapshot = models.UUIDField(editable=False)
    actor_name = models.CharField(max_length=255)
    actor_role = models.CharField(max_length=50)
    actor_barangay = models.CharField(max_length=255, blank=True)
    target_id_snapshot = models.UUIDField(editable=False, db_index=True)
    target_name = models.CharField(max_length=255)
    target_role = models.CharField(max_length=50)
    target_barangay = models.CharField(max_length=255, blank=True)
    target_is_staff = models.BooleanField(default=False)
    barangay_key = models.CharField(max_length=255, blank=True)
    city_key = models.CharField(max_length=255, blank=True)
    province_key = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=30, choices=ACCESS_ACTIONS)
    previous_state = models.JSONField()
    new_state = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        db_table = 'tbl_account_access_audit'
        ordering = ['-created_at', '-log_id']
        default_permissions = ('view',)
        indexes = [
            models.Index(fields=['-created_at', '-log_id'], name='access_audit_recent_idx'),
            models.Index(fields=['barangay_key', 'city_key', 'province_key'], name='access_audit_scope_idx'),
        ]
        constraints = [models.CheckConstraint(
            condition=Q(action__in=[value for value, _ in ACCESS_ACTIONS]), name='access_audit_valid_action')]

    def save(self, *args, **kwargs):
        if not self._state.adding or kwargs.get('update_fields') or kwargs.get('force_update'):
            raise ValidationError('Account access history cannot be edited.')
        # Always INSERT, including when a caller supplies an existing primary key.
        kwargs['force_insert'] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Account access history cannot be deleted.')


def _get_cipher():
    """
        Derives a safe 32-byte Fernet key from Django's SECRET_KEY.
    """
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest())
    return Fernet(key)

class EncryptedTextField(models.TextField):

    def from_db_value(self, value, expressions, connection):
        if not value:
            return value
        
        try:
            return _get_cipher().decrypt(value.encode('utf-8')).decode('utf-8')

        except (InvalidToken, Exception):
            return value

    def to_python(self, value):
        if not value:
            return value
        return str(value)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)

        if not value:
            return value
        return _get_cipher().encrypt(value.encode('utf-8')).decode('utf-8')


MESSAGE_TYPE_CHOICES = [
    ('text', 'Text'),
    ('image', 'Image'),
]

ALLOWED_EMOJIS = {'❤️', '👍', '😂', '😢', '😭', '😮', '😱', '😜', '😡'}


class ChatMessageQuerySet(models.QuerySet):
    def visible_to(self, user):
        return self.filter(is_deleted=False).filter(
            Q(sender_id=user, hidden_by_sender=False) |
            Q(receiver_id=user, hidden_by_receiver=False)
        )


class tbl_chat_message(models.Model):
    chat_message_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    sender_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='sent_messages',
        db_column='sender_id'
    )
    
    receiver_id = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='received_messages',
        db_column='receiver_id'
    )
    
    booking_id = models.ForeignKey(
        'booking.tbl_booking',
        on_delete=models.CASCADE,
        related_name='chat_messages',
        db_column='booking_id',
        null=True,
        blank=True
    )

    is_booking_offer = models.BooleanField(default=False)
    booking_proposal = models.ForeignKey('booking.tbl_booking_proposal', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='offer_messages')

    message_type = models.CharField(
        max_length=10,
        choices=MESSAGE_TYPE_CHOICES,
        default='text'
    )

    image_public_id = models.CharField(
        max_length=500,
        blank=True,
        null=True
    )

    message_payload = EncryptedTextField(
        max_length=500,
        blank=True,
        null=True
    )

    is_read = models.BooleanField(
        default=False
    )

    is_deleted = models.BooleanField(
        default=False
    )

    hidden_by_sender = models.BooleanField(default=False)
    hidden_by_receiver = models.BooleanField(default=False)

    objects = ChatMessageQuerySet.as_manager()

    createdAt = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tbl_chat_message'

        constraints = [
            CheckConstraint(
                condition=~Q(sender_id=F('receiver_id')),
                name='sender_cannot_be_receiver'
            ),

            CheckConstraint(
                condition=(
                    Q(message_type='image') |
                    RawSQL("length(trim(message_payload)) > 0", [], output_field=models.BooleanField())
                ),
                name='message_payload_not_empty_or_whitespace'
            ),

            CheckConstraint(
                condition=(
                    Q(message_type='image') |
                    Q(image_public_id__isnull=True)
                ),
                name='text_message_cannot_have_image_public_id'
            ),
        ]


class tbl_chat_reaction(models.Model):
    reaction_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    message = models.ForeignKey(
        tbl_chat_message,
        on_delete=models.CASCADE,
        related_name='reactions',
        db_column='message_id'
    )
    reactor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='chat_reactions',
        db_column='reactor_id'
    )
    emoji = models.CharField(max_length=10)

    class Meta:
        db_table = 'tbl_chat_reaction'
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'reactor'],
                name='one_reaction_per_user_per_message'
            )
        ]

