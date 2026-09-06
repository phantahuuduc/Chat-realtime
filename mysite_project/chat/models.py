"""
Chat Models — Real-time Team Chat & Notification Platform

8 entities as defined in Plan Section 3 (Data Model / ERD):
- Conversation, ConversationMember, Message, MessageReadReceipt
- UserConnection, PresenceStatus, Notification

Skill applied: database-design (schema design, indexing, constraints)
"""

import secrets
import uuid

from django.conf import settings
from django.db import models


class Conversation(models.Model):
    """
    A chat conversation — either a direct message (1-1) or a group chat.
    last_sequence is used with SELECT FOR UPDATE to assign monotonic
    sequence numbers to messages (see ADR-003).
    """

    TYPE_DIRECT = 'direct'
    TYPE_GROUP = 'group'
    TYPE_CHOICES = [
        (TYPE_DIRECT, 'Direct Message'),
        (TYPE_GROUP, 'Group Chat'),
    ]

    VISIBILITY_PUBLIC = 'public'
    VISIBILITY_PRIVATE = 'private'
    VISIBILITY_CHOICES = [
        (VISIBILITY_PUBLIC, 'Public'),
        (VISIBILITY_PRIVATE, 'Private'),
    ]

    name = models.CharField(max_length=255, blank=True, default='')
    type = models.CharField(max_length=10, choices=TYPE_CHOICES, default=TYPE_GROUP)
    visibility = models.CharField(
        max_length=10, choices=VISIBILITY_CHOICES, default=VISIBILITY_PRIVATE,
    )
    description = models.CharField(max_length=255, blank=True, default='')
    join_code = models.CharField(
        max_length=6, unique=True, null=True, blank=True,
        help_text='Mã tham gia 6 ký tự alphanumeric viết hoa.',
    )
    last_sequence = models.BigIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_conversations',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f'{self.get_type_display()}: {self.name or self.pk}'

    @property
    def group_name(self):
        """Channel Layer group name for this conversation."""
        return f'conversation_{self.pk}'

    @staticmethod
    def group_name_for(conversation_id):
        """Group name không cần load object."""
        return f'conversation_{conversation_id}'

    @staticmethod
    def generate_join_code():
        """Sinh join_code 6 ký tự alphanumeric viết hoa, unique."""
        alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'  # bỏ I O 0 1 dễ nhầm
        for _ in range(50):
            code = ''.join(secrets.choice(alphabet) for _ in range(6))
            if not Conversation.objects.filter(join_code=code).exists():
                return code
        raise RuntimeError('Không sinh được join_code duy nhất.')


class ConversationMember(models.Model):
    """
    Membership in a conversation. Controls who can join the Channels group
    and receive broadcasts (authorization — Plan Section 6).
    """

    ROLE_MEMBER = 'member'
    ROLE_ADMIN = 'admin'
    ROLE_OWNER = 'owner'
    ROLE_CHOICES = [
        (ROLE_MEMBER, 'Member'),
        (ROLE_ADMIN, 'Admin'),
        (ROLE_OWNER, 'Owner'),
    ]

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='members',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='memberships',
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('conversation', 'user')]

    def __str__(self):
        return f'{self.user} in {self.conversation}'


class Message(models.Model):
    """
    A chat message with guaranteed ordering and idempotency.

    - sequence_number: assigned by DB transaction (ADR-003), UNIQUE per conversation
    - client_message_id: UUID from client for idempotency (Plan Section 4.4)

    Constraints (Plan Section 3):
    - UNIQUE(conversation, sequence_number) — ordering integrity
    - UNIQUE(conversation, sender, client_message_id) — idempotency
    - INDEX(conversation, sequence_number) — offline sync queries
    """

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages',
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='sent_messages',
    )
    sequence_number = models.BigIntegerField()
    client_message_id = models.UUIDField(default=uuid.uuid4)
    content = models.TextField(max_length=5000)
    reply_to = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='replies',
    )
    edited_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    is_pinned = models.BooleanField(default=False)
    pinned_at = models.DateTimeField(null=True, blank=True)
    pinned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pinned_messages',
    )
    preview = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['conversation', 'sequence_number'],
                name='unique_conversation_sequence',
            ),
            models.UniqueConstraint(
                fields=['conversation', 'sender', 'client_message_id'],
                name='unique_idempotency_key',
            ),
        ]
        indexes = [
            models.Index(
                fields=['conversation', 'sequence_number'],
                name='idx_conversation_sequence',
            ),
        ]
        ordering = ['sequence_number']

    def __str__(self):
        return f'Msg #{self.sequence_number} in {self.conversation_id}'


class MessageReaction(models.Model):
    """
    Reaction emoji trên một message.
    UNIQUE(message, user, emoji) — bấm lại là toggle, không tạo dòng thứ 2.
    """

    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name='reactions',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='reactions',
    )
    emoji = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'user', 'emoji'],
                name='unique_message_reaction',
            ),
        ]
        indexes = [
            models.Index(fields=['message'], name='idx_reaction_message'),
        ]

    def __str__(self):
        return f'{self.user} {self.emoji} on {self.message_id}'


class MessageReadReceipt(models.Model):
    """
    Tracks which user has read which message.
    UNIQUE(message, user) prevents duplicate read receipts on client retry.
    """

    message = models.ForeignKey(
        Message, on_delete=models.CASCADE, related_name='read_receipts',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='read_receipts',
    )
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'user'],
                name='unique_read_receipt',
            ),
        ]

    def __str__(self):
        return f'{self.user} read {self.message}'


class UserConnection(models.Model):
    """
    Tracks active WebSocket connections per user per device.
    Multi-device presence: user is only offline when ALL connections are closed.
    INDEX on last_heartbeat for timeout scan job (Plan Section 3).
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='connections',
    )
    device_id = models.CharField(max_length=255, default='default')
    channel_name = models.CharField(max_length=255, unique=True)
    connected_at = models.DateTimeField(auto_now_add=True)
    last_heartbeat = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'last_heartbeat'], name='idx_user_heartbeat'),
        ]

    def __str__(self):
        return f'{self.user} @ {self.channel_name}'


class PresenceStatus(models.Model):
    """
    Presence state machine: online ⇄ away ⇄ offline (Plan Section 4.1).
    One record per user. Updated by heartbeat/connection lifecycle.
    """

    STATUS_ONLINE = 'online'
    STATUS_AWAY = 'away'
    STATUS_OFFLINE = 'offline'
    STATUS_CHOICES = [
        (STATUS_ONLINE, 'Online'),
        (STATUS_AWAY, 'Away'),
        (STATUS_OFFLINE, 'Offline'),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='presence',
    )
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_OFFLINE,
    )
    last_changed = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.user}: {self.status}'


class Notification(models.Model):
    """
    Real-time notifications delivered via dedicated user channel (user_{id}).
    Independent from conversation groups (Plan Section 2.3).
    """

    TYPE_NEW_MESSAGE = 'new_message'
    TYPE_MENTION = 'mention'
    TYPE_ADDED_TO_GROUP = 'added_to_group'
    TYPE_CHOICES = [
        (TYPE_NEW_MESSAGE, 'New Message'),
        (TYPE_MENTION, 'Mention'),
        (TYPE_ADDED_TO_GROUP, 'Added to Group'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='notifications',
    )
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    title = models.CharField(max_length=255)
    body = models.TextField(blank=True, default='')
    data = models.JSONField(default=dict, blank=True)
    read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'read', '-created_at'], name='idx_user_notifications'),
        ]

    def __str__(self):
        return f'{self.type} for {self.user}'
