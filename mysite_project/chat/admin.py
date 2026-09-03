"""
Chat Admin — Register models for Django admin panel.
Plan Section 2.2: Admin/Staff management.
"""

from django.contrib import admin

from .models import (
    Conversation,
    ConversationMember,
    Message,
    MessageReadReceipt,
    Notification,
    PresenceStatus,
    UserConnection,
)


class ConversationMemberInline(admin.TabularInline):
    model = ConversationMember
    extra = 0


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'type', 'last_sequence', 'created_at']
    list_filter = ['type']
    inlines = [ConversationMemberInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['id', 'conversation', 'sender', 'sequence_number', 'content_preview', 'created_at']
    list_filter = ['conversation']
    search_fields = ['content']

    @admin.display(description='Content')
    def content_preview(self, obj):
        return obj.content[:80]


@admin.register(UserConnection)
class UserConnectionAdmin(admin.ModelAdmin):
    list_display = ['user', 'device_id', 'channel_name', 'connected_at', 'last_heartbeat']
    list_filter = ['user']


@admin.register(PresenceStatus)
class PresenceStatusAdmin(admin.ModelAdmin):
    list_display = ['user', 'status', 'last_changed']
    list_filter = ['status']


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ['user', 'type', 'title', 'read', 'created_at']
    list_filter = ['type', 'read']
