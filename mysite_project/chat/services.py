"""
Service layer — mọi thao tác ghi DB của chat nằm ở đây, gọi được từ cả
consumer (qua database_sync_to_async) lẫn REST view.

Hai điểm cốt lõi:
- `create_message` kiểm tra idempotency TRƯỚC khi cấp sequence (mục 5.2).
- Sequence cấp bằng MỘT câu UPDATE ... RETURNING atomic ở cuối transaction (mục 5.3).
"""

import bleach
import structlog
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from .models import (
    Conversation,
    ConversationMember,
    Message,
    MessageReaction,
    MessageReadReceipt,
)

logger = structlog.get_logger('chat')

QUICK_REACTIONS = ['\U0001F44D', '❤️', '\U0001F602', '\U0001F62E', '\U0001F622', '\U0001F525']


# ---------------------------------------------------------------------------
# Serialize helpers
# ---------------------------------------------------------------------------

def serialize_message(message, reactions=None):
    """Payload chuẩn của một message dùng chung cho WS và REST."""
    reply = None
    if message.reply_to_id and message.reply_to is not None:
        parent = message.reply_to
        reply = {
            'id': parent.pk,
            'sequence_number': parent.sequence_number,
            'sender_username': parent.sender.username,
            'content': '' if parent.is_deleted else parent.content[:140],
            'is_deleted': parent.is_deleted,
        }
    return {
        'id': message.pk,
        'conversation_id': message.conversation_id,
        'sender_id': message.sender_id,
        'sender_username': message.sender.username,
        'sequence_number': message.sequence_number,
        'client_message_id': str(message.client_message_id),
        'content': '' if message.is_deleted else message.content,
        'is_deleted': message.is_deleted,
        'is_pinned': message.is_pinned,
        'preview': None if message.is_deleted else message.preview,
        'edited_at': message.edited_at.isoformat() if message.edited_at else None,
        'reply_to': reply,
        'reactions': reactions if reactions is not None else reaction_summary(message.pk),
        'created_at': message.created_at.isoformat(),
    }


def reaction_summary(message_id):
    """[{emoji, count, user_ids}] cho một message."""
    rows = MessageReaction.objects.filter(message_id=message_id).values_list(
        'emoji', 'user_id',
    )
    buckets = {}
    for emoji, user_id in rows:
        buckets.setdefault(emoji, []).append(user_id)
    return [
        {'emoji': emoji, 'count': len(uids), 'user_ids': uids}
        for emoji, uids in sorted(buckets.items())
    ]


def serialize_conversation(conv, user=None):
    last = conv.messages.select_related('sender').order_by('-sequence_number').first()
    return {
        'id': conv.pk,
        'name': conv.name or f'Room #{conv.pk}',
        'type': conv.type,
        'visibility': conv.visibility,
        'description': conv.description,
        'join_code': conv.join_code,
        'last_sequence': conv.last_sequence,
        'member_count': conv.members.count(),
        'last_message': (
            {
                'content': '' if last.is_deleted else last.content[:80],
                'sender_username': last.sender.username,
                'created_at': last.created_at.isoformat(),
                'sequence_number': last.sequence_number,
            }
            if last else None
        ),
        'unread_count': unread_count(conv, user) if user else 0,
        'created_at': conv.created_at.isoformat(),
        'updated_at': conv.updated_at.isoformat(),
    }


def unread_count(conv, user):
    last_read = (
        MessageReadReceipt.objects
        .filter(message__conversation=conv, user=user)
        .order_by('-message__sequence_number')
        .values_list('message__sequence_number', flat=True)
        .first()
    ) or 0
    return (
        conv.messages
        .filter(sequence_number__gt=last_read, is_deleted=False)
        .exclude(sender=user)
        .count()
    )


# ---------------------------------------------------------------------------
# Sequence — mục 5.3
# ---------------------------------------------------------------------------

def _next_sequence(conversation_id):
    """
    Cấp sequence bằng MỘT câu lệnh atomic, không SELECT ... FOR UPDATE + UPDATE.
    Gọi ở CUỐI transaction, sau mọi validation, để giữ khoá row ngắn nhất.
    """
    table = Conversation._meta.db_table
    now = timezone.now()
    with connection.cursor() as cursor:
        if connection.vendor == 'postgresql':
            cursor.execute(
                f'UPDATE "{table}" SET last_sequence = last_sequence + 1, '
                f'updated_at = %s WHERE id = %s RETURNING last_sequence',
                [now, conversation_id],
            )
            row = cursor.fetchone()
            if row is None:
                raise Conversation.DoesNotExist(conversation_id)
            return row[0]

        # SQLite (test/dev): UPDATE rồi SELECT trong cùng transaction.
        cursor.execute(
            f'UPDATE "{table}" SET last_sequence = last_sequence + 1, '
            f'updated_at = %s WHERE id = %s',
            [now, conversation_id],
        )
        cursor.execute(
            f'SELECT last_sequence FROM "{table}" WHERE id = %s', [conversation_id],
        )
        row = cursor.fetchone()
        if row is None:
            raise Conversation.DoesNotExist(conversation_id)
        return row[0]


# ---------------------------------------------------------------------------
# Message write — mục 5.2
# ---------------------------------------------------------------------------

def _fetch_existing(conversation_id, user_id, client_message_id):
    return (
        Message.objects
        .filter(
            conversation_id=conversation_id,
            sender_id=user_id,
            client_message_id=client_message_id,
        )
        .select_related('sender', 'reply_to', 'reply_to__sender')
        .first()
    )


def create_message(conversation_id, user, content, client_message_id, reply_to_id=None):
    """
    Trả (payload, created).
    created=False nghĩa là trùng client_message_id — KHÔNG cấp sequence mới,
    KHÔNG broadcast lại, chỉ trả bản ghi đã có.

    Thứ tự (mục 5.2):
      1. SELECT theo (conversation, sender, client_message_id)
      2. Có -> trả bản cũ
      3. Chưa -> transaction: validate, cấp sequence, INSERT
      4. IntegrityError -> SELECT lại, trả bản đã có
    """
    existing = _fetch_existing(conversation_id, user.pk, client_message_id)
    if existing is not None:
        logger.info(
            'message.duplicate_ignored',
            client_message_id=str(client_message_id),
            existing_sequence=existing.sequence_number,
        )
        return serialize_message(existing), False

    content = bleach.clean(content, tags=[], strip=True)

    try:
        with transaction.atomic():
            # Validation trước, cấp sequence sau cùng.
            parent = None
            if reply_to_id:
                parent = (
                    Message.objects
                    .filter(pk=reply_to_id, conversation_id=conversation_id)
                    .first()
                )
            sequence = _next_sequence(conversation_id)
            message = Message.objects.create(
                conversation_id=conversation_id,
                sender=user,
                sequence_number=sequence,
                client_message_id=client_message_id,
                content=content,
                reply_to=parent,
            )
    except IntegrityError:
        # Race: hai request cùng client_message_id vào song song.
        existing = _fetch_existing(conversation_id, user.pk, client_message_id)
        if existing is not None:
            logger.info(
                'message.duplicate_race',
                client_message_id=str(client_message_id),
                existing_sequence=existing.sequence_number,
            )
            return serialize_message(existing), False
        raise

    message = (
        Message.objects
        .select_related('sender', 'reply_to', 'reply_to__sender')
        .get(pk=message.pk)
    )
    return serialize_message(message, reactions=[]), True


def edit_message(message_id, user, content):
    """Chỉ tác giả, trong MESSAGE_EDIT_WINDOW_SECONDS. Trả (payload, error_code)."""
    message = (
        Message.objects
        .select_related('sender', 'reply_to', 'reply_to__sender')
        .filter(pk=message_id)
        .first()
    )
    if message is None:
        return None, 'MESSAGE_NOT_FOUND'
    if message.sender_id != user.pk:
        return None, 'FORBIDDEN'
    if message.is_deleted:
        return None, 'MESSAGE_DELETED'
    age = (timezone.now() - message.created_at).total_seconds()
    if age > settings.MESSAGE_EDIT_WINDOW_SECONDS:
        return None, 'EDIT_WINDOW_EXPIRED'

    message.content = bleach.clean(content, tags=[], strip=True)
    message.edited_at = timezone.now()
    message.save(update_fields=['content', 'edited_at'])
    return serialize_message(message), None


def delete_message(message_id, user):
    """
    Soft delete. Tác giả xoá tin của mình; owner/admin xoá mọi tin.
    Trả (payload, error_code).
    """
    message = (
        Message.objects
        .select_related('sender', 'conversation')
        .filter(pk=message_id)
        .first()
    )
    if message is None:
        return None, 'MESSAGE_NOT_FOUND'

    if message.sender_id != user.pk:
        role = member_role(message.conversation_id, user.pk)
        if role not in (ConversationMember.ROLE_OWNER, ConversationMember.ROLE_ADMIN):
            return None, 'FORBIDDEN'

    message.is_deleted = True
    message.content = ''
    message.is_pinned = False
    message.preview = None
    message.save(update_fields=['is_deleted', 'content', 'is_pinned', 'preview'])
    return {
        'message_id': message.pk,
        'conversation_id': message.conversation_id,
    }, None


def set_pinned(message_id, user, pinned):
    """
    Ghim / bỏ ghim. Owner/admin ghim mọi tin; tác giả ghim tin của mình.
    Trả (payload, error_code).
    """
    message = (
        Message.objects
        .select_related('sender')
        .filter(pk=message_id, is_deleted=False)
        .first()
    )
    if message is None:
        return None, 'MESSAGE_NOT_FOUND'

    role = member_role(message.conversation_id, user.pk)
    is_staff = role in (ConversationMember.ROLE_OWNER, ConversationMember.ROLE_ADMIN)
    if message.sender_id != user.pk and not is_staff:
        return None, 'FORBIDDEN'

    message.is_pinned = pinned
    message.pinned_at = timezone.now() if pinned else None
    message.pinned_by = user if pinned else None
    message.save(update_fields=['is_pinned', 'pinned_at', 'pinned_by'])

    return {
        'message_id': message.pk,
        'conversation_id': message.conversation_id,
        'is_pinned': pinned,
        'pinned_by': user.username if pinned else None,
    }, None


def save_preview(message_id, preview):
    """Ghi preview đã fetch được. Trả payload để broadcast, None nếu bỏ qua."""
    message = Message.objects.filter(pk=message_id, is_deleted=False).first()
    if message is None or not preview:
        return None
    message.preview = preview
    message.save(update_fields=['preview'])
    return {
        'message_id': message.pk,
        'conversation_id': message.conversation_id,
        'preview': preview,
    }


def pinned_messages(conversation_id):
    rows = (
        Message.objects
        .filter(conversation_id=conversation_id, is_pinned=True, is_deleted=False)
        .select_related('sender', 'reply_to', 'reply_to__sender')
        .order_by('pinned_at')
    )
    return [serialize_message(m) for m in rows]


def toggle_reaction(message_id, user, emoji):
    """Bấm lại là gỡ. Trả (payload, error_code)."""
    if emoji not in QUICK_REACTIONS:
        return None, 'INVALID_EMOJI'
    message = Message.objects.filter(pk=message_id).first()
    if message is None:
        return None, 'MESSAGE_NOT_FOUND'
    if not is_member(message.conversation_id, user.pk):
        return None, 'FORBIDDEN'

    existing = MessageReaction.objects.filter(
        message_id=message_id, user=user, emoji=emoji,
    ).first()
    if existing:
        existing.delete()
        action = 'removed'
    else:
        try:
            MessageReaction.objects.create(
                message_id=message_id, user=user, emoji=emoji,
            )
        except IntegrityError:
            pass
        action = 'added'

    return {
        'message_id': message_id,
        'conversation_id': message.conversation_id,
        'emoji': emoji,
        'user_id': user.pk,
        'username': user.username,
        'action': action,
        'reactions': reaction_summary(message_id),
    }, None


def mark_read(conversation_id, user, sequence_number):
    """
    Upsert read receipt tới sequence_number cao nhất đã đọc.
    Trả sequence thực sự ghi nhận, None nếu không có gì để ghi.
    """
    message = (
        Message.objects
        .filter(conversation_id=conversation_id, sequence_number__lte=sequence_number)
        .exclude(sender=user)
        .order_by('-sequence_number')
        .first()
    )
    if message is None:
        return None
    MessageReadReceipt.objects.get_or_create(message=message, user=user)
    return message.sequence_number


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def is_member(conversation_id, user_id):
    return ConversationMember.objects.filter(
        conversation_id=conversation_id, user_id=user_id,
    ).exists()


def member_role(conversation_id, user_id):
    return (
        ConversationMember.objects
        .filter(conversation_id=conversation_id, user_id=user_id)
        .values_list('role', flat=True)
        .first()
    )


def sync_messages(conversation_id, after_sequence):
    """Offline sync — tối đa OFFLINE_SYNC_LIMIT message kèm cờ has_more (mục 5.9)."""
    limit = settings.OFFLINE_SYNC_LIMIT
    rows = list(
        Message.objects
        .filter(conversation_id=conversation_id, sequence_number__gt=after_sequence)
        .select_related('sender', 'reply_to', 'reply_to__sender')
        .order_by('sequence_number')[:limit + 1]
    )
    has_more = len(rows) > limit
    return [serialize_message(m) for m in rows[:limit]], has_more


def member_payloads(conversation_id):
    """Danh sách thành viên (chưa gắn presence)."""
    members = (
        ConversationMember.objects
        .filter(conversation_id=conversation_id)
        .select_related('user')
        .order_by('user__username')
    )
    return [
        {
            'user_id': m.user_id,
            'username': m.user.username,
            'role': m.role,
            'joined_at': m.joined_at.isoformat(),
        }
        for m in members
    ]
