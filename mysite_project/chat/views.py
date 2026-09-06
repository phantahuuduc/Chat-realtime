"""
Chat REST API.

Phân quyền theo membership. Mọi broadcast sau khi ghi DB đều đi qua
`broadcast_on_commit` để chỉ bắn sau khi transaction commit (mục 5.1).
"""

import structlog
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from rest_framework import generics, permissions, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .broadcast import broadcast_on_commit
from .consumers import envelope, user_group
from .models import Conversation, ConversationMember, Message, Notification
from .serializers import (
    ConversationCreateSerializer,
    ConversationSerializer,
    JoinCodeSerializer,
    MessageSerializer,
    NotificationSerializer,
)

logger = structlog.get_logger('chat')
User = get_user_model()


class IsMember(permissions.BasePermission):
    """Chỉ thành viên của conversation mới đọc được dữ liệu phòng."""

    def has_object_permission(self, request, view, obj):
        conv = obj if isinstance(obj, Conversation) else getattr(obj, 'conversation', None)
        if conv is None:
            return False
        return services.is_member(conv.pk, request.user.pk)


def _require_membership(user, conversation_id):
    conv = Conversation.objects.filter(pk=conversation_id).first()
    if conv is None:
        return None, Response({'error': 'Phòng không tồn tại.'}, status=404)
    if not services.is_member(conversation_id, user.pk):
        return None, Response({'error': 'Bạn không có quyền vào phòng này.'}, status=403)
    return conv, None


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

class ConversationListCreateView(generics.ListCreateAPIView):
    """
    GET  /api/conversations/ — danh sách phòng của user.
    POST /api/conversations/ — tạo phòng mới (sinh join_code, creator là owner).
    """
    serializer_class = ConversationSerializer

    def get_queryset(self):
        return (
            Conversation.objects
            .filter(members__user=self.request.user)
            .prefetch_related('members__user')
            .distinct()
        )

    def create(self, request, *args, **kwargs):
        s = ConversationCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data

        with transaction.atomic():
            conv = Conversation.objects.create(
                name=d.get('name', ''),
                type=d['type'],
                visibility=d.get('visibility', Conversation.VISIBILITY_PRIVATE),
                description=d.get('description', ''),
                join_code=Conversation.generate_join_code(),
                created_by=request.user,
            )
            ConversationMember.objects.create(
                conversation=conv, user=request.user,
                role=ConversationMember.ROLE_OWNER,
            )
            for uid in d.get('member_ids', []):
                if uid != request.user.pk and User.objects.filter(pk=uid).exists():
                    ConversationMember.objects.get_or_create(
                        conversation=conv, user_id=uid,
                        defaults={'role': ConversationMember.ROLE_MEMBER},
                    )
            for uname in d.get('member_usernames', []):
                uname = uname.strip()
                if not uname or uname == request.user.username:
                    continue
                other = User.objects.filter(username=uname).first()
                if other:
                    ConversationMember.objects.get_or_create(
                        conversation=conv, user=other,
                        defaults={'role': ConversationMember.ROLE_MEMBER},
                    )

            payload = services.serialize_conversation(conv, request.user)
            for member_id in conv.members.values_list('user_id', flat=True):
                broadcast_on_commit(
                    user_group(member_id),
                    {'type': 'fanout',
                     'envelope': envelope('room.created', {'conversation': payload})},
                )

        return Response(
            ConversationSerializer(conv, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class ConversationDetailView(generics.RetrieveAPIView):
    """GET /api/conversations/<id>/"""
    serializer_class = ConversationSerializer
    permission_classes = [permissions.IsAuthenticated, IsMember]

    def get_queryset(self):
        return Conversation.objects.prefetch_related('members__user')


class PublicRoomsView(APIView):
    """GET /api/rooms/public/ — danh sách phòng public, phân trang."""

    def get(self, request):
        qs = (
            Conversation.objects
            .filter(visibility=Conversation.VISIBILITY_PUBLIC)
            .order_by('-updated_at')
        )
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(qs, request, view=self)
        joined = set(
            ConversationMember.objects
            .filter(user=request.user, conversation__in=page)
            .values_list('conversation_id', flat=True)
        )
        data = []
        for conv in page:
            row = services.serialize_conversation(conv, request.user)
            row['joined'] = conv.pk in joined
            row.pop('join_code', None)
            data.append(row)
        return paginator.get_paginated_response(data)


class RoomJoinView(APIView):
    """POST /api/rooms/join/ — tham gia bằng join_code."""

    def post(self, request):
        s = JoinCodeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        code = (s.validated_data.get('join_code') or '').strip().upper()
        conversation_id = s.validated_data.get('conversation_id')

        if code:
            conv = Conversation.objects.filter(join_code=code).first()
            if conv is None:
                return Response({'error': 'Mã phòng không tồn tại.'}, status=404)
        else:
            # Không có mã: chỉ vào được phòng công khai.
            conv = Conversation.objects.filter(
                pk=conversation_id, visibility=Conversation.VISIBILITY_PUBLIC,
            ).first()
            if conv is None:
                return Response(
                    {'error': 'Phòng không công khai — cần mã tham gia.'}, status=403,
                )

        with transaction.atomic():
            member, created = ConversationMember.objects.get_or_create(
                conversation=conv, user=request.user,
                defaults={'role': ConversationMember.ROLE_MEMBER},
            )
            payload = services.serialize_conversation(conv, request.user)
            if created:
                broadcast_on_commit(
                    conv.group_name,
                    {'type': 'fanout', 'envelope': envelope('member.joined', {
                        'conversation_id': conv.pk,
                        'user_id': request.user.pk,
                        'username': request.user.username,
                        'role': member.role,
                    })},
                )
                broadcast_on_commit(
                    user_group(request.user.pk),
                    {'type': 'fanout',
                     'envelope': envelope('room.created', {'conversation': payload})},
                )

        return Response({'conversation': payload, 'joined': created})


class RoomLeaveView(APIView):
    """POST /api/rooms/<id>/leave/"""

    def post(self, request, conversation_id):
        conv, error = _require_membership(request.user, conversation_id)
        if error:
            return error

        with transaction.atomic():
            ConversationMember.objects.filter(
                conversation=conv, user=request.user,
            ).delete()
            broadcast_on_commit(
                conv.group_name,
                {'type': 'fanout', 'envelope': envelope('member.left', {
                    'conversation_id': conv.pk,
                    'user_id': request.user.pk,
                    'username': request.user.username,
                })},
            )
            broadcast_on_commit(
                user_group(request.user.pk),
                {'type': 'fanout',
                 'envelope': envelope('room.deleted', {'conversation_id': conv.pk})},
            )
        return Response({'left': True, 'conversation_id': conv.pk})


class RoomMemberListView(APIView):
    """GET /api/rooms/<id>/members/ — kèm presence, online xếp trước."""

    ORDER = {'online': 0, 'away': 1, 'offline': 2}

    def get(self, request, conversation_id):
        conv, error = _require_membership(request.user, conversation_id)
        if error:
            return error

        members = services.member_payloads(conversation_id)
        statuses = dict(
            User.objects
            .filter(pk__in=[m['user_id'] for m in members])
            .values_list('pk', 'presence__status')
        )
        for m in members:
            m['status'] = statuses.get(m['user_id']) or 'offline'
        members.sort(key=lambda m: (self.ORDER.get(m['status'], 3), m['username']))
        return Response({
            'conversation_id': conv.pk,
            'my_role': services.member_role(conversation_id, request.user.pk),
            'members': members,
        })


class RoomMemberRemoveView(APIView):
    """POST /api/rooms/<id>/members/<user_id>/remove/ — chỉ owner/admin."""

    def post(self, request, conversation_id, user_id):
        conv, error = _require_membership(request.user, conversation_id)
        if error:
            return error

        role = services.member_role(conversation_id, request.user.pk)
        if role not in (ConversationMember.ROLE_OWNER, ConversationMember.ROLE_ADMIN):
            return Response({'error': 'Chỉ owner/admin được đá thành viên.'}, status=403)

        target = ConversationMember.objects.filter(
            conversation=conv, user_id=user_id,
        ).select_related('user').first()
        if target is None:
            return Response({'error': 'Thành viên không tồn tại.'}, status=404)
        if target.role == ConversationMember.ROLE_OWNER:
            return Response({'error': 'Không thể đá owner.'}, status=403)

        username = target.user.username
        with transaction.atomic():
            target.delete()
            broadcast_on_commit(
                conv.group_name,
                {'type': 'fanout', 'envelope': envelope('member.left', {
                    'conversation_id': conv.pk,
                    'user_id': int(user_id),
                    'username': username,
                    'removed_by': request.user.username,
                })},
            )
            # Buộc client bị đá rời phòng: kênh riêng nhận room.deleted.
            broadcast_on_commit(
                user_group(int(user_id)),
                {'type': 'fanout', 'envelope': envelope('room.deleted', {
                    'conversation_id': conv.pk,
                    'reason': 'removed',
                })},
            )
        return Response({'removed': True, 'user_id': int(user_id)})


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class MessageListView(generics.ListAPIView):
    """
    GET /api/conversations/<id>/messages/
        ?after_sequence=N   — offline sync (tăng dần)
        ?before_sequence=N  — tải tin cũ khi cuộn lên
        ?limit=50
    """
    serializer_class = MessageSerializer

    def get_queryset(self):
        conv_id = self.kwargs['conversation_id']
        if not services.is_member(conv_id, self.request.user.pk):
            return Message.objects.none()
        qs = Message.objects.filter(conversation_id=conv_id).select_related(
            'sender', 'reply_to', 'reply_to__sender',
        )
        after = self.request.query_params.get('after_sequence')
        if after:
            qs = qs.filter(sequence_number__gt=int(after))
        before = self.request.query_params.get('before_sequence')
        if before:
            qs = qs.filter(sequence_number__lt=int(before))
        return qs.order_by('sequence_number')

    def list(self, request, *args, **kwargs):
        conv_id = self.kwargs['conversation_id']
        if not services.is_member(conv_id, request.user.pk):
            return Response({'results': [], 'has_more': False}, status=403)

        try:
            limit = min(int(request.query_params.get('limit', 50)), 200)
        except (TypeError, ValueError):
            limit = 50

        qs = self.get_queryset()
        before = request.query_params.get('before_sequence')
        if before:
            # Trang cũ: lấy `limit` bản ghi NGAY TRƯỚC before_sequence.
            rows = list(qs.order_by('-sequence_number')[:limit + 1])
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
        else:
            rows = list(qs.order_by('-sequence_number')[:limit + 1])
            has_more = len(rows) > limit
            rows = list(reversed(rows[:limit]))

        return Response({
            'results': [services.serialize_message(m) for m in rows],
            'has_more': has_more,
        })


class MessageSearchView(APIView):
    """GET /api/rooms/<id>/messages/search/?q="""

    def get(self, request, conversation_id):
        conv, error = _require_membership(request.user, conversation_id)
        if error:
            return error

        q = (request.query_params.get('q') or '').strip()
        if len(q) < 2:
            return Response({'results': [], 'query': q})

        rows = (
            Message.objects
            .filter(conversation=conv, is_deleted=False)
            .filter(Q(content__icontains=q))
            .select_related('sender', 'reply_to', 'reply_to__sender')
            .order_by('-sequence_number')[:50]
        )
        return Response({
            'query': q,
            'results': [services.serialize_message(m) for m in rows],
        })


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

class NotificationListView(generics.ListAPIView):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user)


class NotificationMarkReadView(APIView):
    def post(self, request):
        count = Notification.objects.filter(
            user=request.user, read=False,
        ).update(read=True)
        return Response({'marked_read': count})
