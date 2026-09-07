"""
Chat Serializers — REST API data validation and serialization.
Skill: api-security-best-practices (input validation, sanitization)
"""

import bleach
from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    Conversation,
    ConversationMember,
    Message,
    MessageReadReceipt,
    Notification,
    PresenceStatus,
)

User = get_user_model()


class UserSerializer(serializers.ModelSerializer):
    presence = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'last_name', 'presence']
        read_only_fields = fields

    def get_presence(self, obj):
        try:
            return obj.presence.status
        except PresenceStatus.DoesNotExist:
            return 'offline'


class ConversationMemberSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = ConversationMember
        fields = ['id', 'user', 'role', 'joined_at']
        read_only_fields = ['id', 'joined_at']


class MessageSerializer(serializers.ModelSerializer):
    sender = UserSerializer(read_only=True)

    class Meta:
        model = Message
        fields = [
            'id', 'conversation', 'sender', 'sequence_number',
            'client_message_id', 'content', 'reply_to', 'edited_at',
            'is_deleted', 'is_pinned', 'preview', 'created_at',
        ]
        read_only_fields = ['id', 'sequence_number', 'created_at']

    def validate_content(self, value):
        """Sanitize message content to prevent XSS (Plan Section 6)."""
        return bleach.clean(value, tags=[], strip=True)


class ConversationSerializer(serializers.ModelSerializer):
    members = ConversationMemberSerializer(many=True, read_only=True)
    last_message = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = [
            'id', 'name', 'type', 'visibility', 'description', 'join_code', 'is_ai',
            'members', 'last_message', 'unread_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def get_last_message(self, obj):
        msg = obj.messages.order_by('-sequence_number').first()
        if msg:
            return MessageSerializer(msg).data
        return None

    def get_unread_count(self, obj):
        request = self.context.get('request')
        if not request or not request.user.is_authenticated:
            return 0
        last_read = MessageReadReceipt.objects.filter(
            message__conversation=obj, user=request.user,
        ).order_by('-message__sequence_number').first()
        base = obj.messages.filter(is_deleted=False).exclude(sender=request.user)
        if last_read:
            return base.filter(
                sequence_number__gt=last_read.message.sequence_number,
            ).count()
        return base.count()


class ConversationCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False, default='')
    type = serializers.ChoiceField(choices=Conversation.TYPE_CHOICES, default='group')
    visibility = serializers.ChoiceField(
        choices=Conversation.VISIBILITY_CHOICES, default='private',
    )
    description = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default='',
    )
    member_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
    )
    member_usernames = serializers.ListField(
        child=serializers.CharField(), required=False, default=list,
    )


class JoinCodeSerializer(serializers.Serializer):
    """Tham gia bằng mã 6 ký tự, hoặc bằng id với phòng public."""
    join_code = serializers.CharField(
        min_length=6, max_length=6, required=False, allow_blank=True,
    )
    conversation_id = serializers.IntegerField(required=False)

    def validate(self, attrs):
        if not attrs.get('join_code') and not attrs.get('conversation_id'):
            raise serializers.ValidationError('Cần join_code hoặc conversation_id.')
        return attrs


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ['id', 'type', 'title', 'body', 'data', 'read', 'created_at']
        read_only_fields = ['id', 'type', 'title', 'body', 'data', 'created_at']
