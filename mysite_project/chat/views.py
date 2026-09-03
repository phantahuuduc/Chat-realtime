"""
Chat REST API views.
Skill: api-security-best-practices (authorization per membership)
"""

from django.db import models
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Conversation, ConversationMember, Message, Notification
from .serializers import (
    ConversationCreateSerializer,
    ConversationSerializer,
    MessageSerializer,
    NotificationSerializer,
)


class IsMember(permissions.BasePermission):
    """Only conversation members can access conversation data."""

    def has_object_permission(self, request, view, obj):
        conv = obj if isinstance(obj, Conversation) else getattr(obj, 'conversation', None)
        if conv is None:
            return False
        return ConversationMember.objects.filter(
            conversation=conv, user=request.user,
        ).exists()


class ConversationListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/conversations/ — List conversations the user belongs to.
    POST /api/conversations/ — Create a new conversation and add members.
    """
    serializer_class = ConversationSerializer

    def get_queryset(self):
        return Conversation.objects.filter(
            members__user=self.request.user,
        ).prefetch_related('members__user').distinct()

    def create(self, request, *args, **kwargs):
        s = ConversationCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        conv = Conversation.objects.create(
            name=d.get('name', ''),
            type=d['type'],
            created_by=request.user,
        )
        # Add creator as owner
        ConversationMember.objects.create(
            conversation=conv, user=request.user, role='owner',
        )
        # Add other members
        from django.contrib.auth import get_user_model
        User = get_user_model()
        for uid in d.get('member_ids', []):
            if uid != request.user.pk:
                try:
                    user = User.objects.get(pk=uid)
                    ConversationMember.objects.create(
                        conversation=conv, user=user, role='member',
                    )
                except User.DoesNotExist:
                    pass

        for uname in d.get('member_usernames', []):
            uname = uname.strip()
            if uname and uname != request.user.username:
                try:
                    user = User.objects.get(username=uname)
                    ConversationMember.objects.get_or_create(
                        conversation=conv, user=user, defaults={'role': 'member'},
                    )
                except User.DoesNotExist:
                    pass

        return Response(
            ConversationSerializer(conv, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class ConversationDetailView(generics.RetrieveAPIView):
    """GET /api/conversations/{id}/ — Get conversation detail."""
    serializer_class = ConversationSerializer
    permission_classes = [permissions.IsAuthenticated, IsMember]

    def get_queryset(self):
        return Conversation.objects.prefetch_related('members__user')


class MessageListView(generics.ListAPIView):
    """
    GET /api/conversations/{conversation_id}/messages/ — Chat history.
    Supports offline sync via ?after_sequence=N query param.
    """
    serializer_class = MessageSerializer

    def get_queryset(self):
        conv_id = self.kwargs['conversation_id']
        # Authorization: check membership
        if not ConversationMember.objects.filter(
            conversation_id=conv_id, user=self.request.user,
        ).exists():
            return Message.objects.none()

        qs = Message.objects.filter(conversation_id=conv_id).select_related('sender')
        after = self.request.query_params.get('after_sequence')
        if after:
            qs = qs.filter(sequence_number__gt=int(after))
        return qs.order_by('sequence_number')


class NotificationListView(generics.ListAPIView):
    """GET /api/notifications/ — List user's notifications."""
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)


class NotificationMarkReadView(APIView):
    """POST /api/notifications/mark-read/ — Mark all notifications as read."""

    def post(self, request):
        count = Notification.objects.filter(
            user=request.user, read=False,
        ).update(read=True)
        return Response({'marked_read': count})
