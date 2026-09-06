"""
Chat Consumers — logic real-time.

Envelope thống nhất hai chiều (mục 5.11):

    { "type": "message.new", "payload": { ... }, "client_message_id": "uuid|null" }

Topology group (mục 5.8): mỗi socket chỉ join kênh riêng `user_<id>` và ĐÚNG
một phòng đang mở. Đổi phòng -> group_discard phòng cũ, group_add phòng mới.

Mọi truy vấn ORM đều đi qua `database_sync_to_async` (mục 5.4).
Mọi group_send đều đi qua `safe_group_send` (mục 5.10).
"""

import asyncio
import time
import uuid

import structlog
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings

from . import linkpreview, presence, services
from .broadcast import safe_group_send
from .middleware import WS_SUBPROTOCOL
from .models import Conversation, PresenceStatus

logger = structlog.get_logger('chat')

# Close codes riêng
CLOSE_UNAUTHENTICATED = 4001
CLOSE_FORBIDDEN = 4003


def envelope(msg_type, payload=None, client_message_id=None):
    """Dựng envelope chuẩn."""
    return {
        'type': msg_type,
        'payload': payload or {},
        'client_message_id': client_message_id,
    }


def user_group(user_id):
    return f'user_{user_id}'


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """
    Một socket duy nhất cho cả phiên:
    - kênh `user_<id>`: presence, notification liên phòng, room.created/deleted
    - phòng đang mở: message/typing/read/reaction

    `conversation_id` trong URL là tuỳ chọn; có thì mở sẵn phòng đó khi connect.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = None
        self.device_id = None
        self.conversation_id = None
        self.conversation_group = None
        self._message_timestamps = []

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def connect(self):
        self.user = self.scope.get('user')
        if not self.user or not self.user.is_authenticated:
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return

        subprotocol = (
            WS_SUBPROTOCOL
            if WS_SUBPROTOCOL in (self.scope.get('subprotocols') or [])
            else None
        )
        await self.accept(subprotocol)

        self.device_id = self.channel_name.rsplit('!', 1)[-1] or self.channel_name
        await self.channel_layer.group_add(user_group(self.user.pk), self.channel_name)

        await presence.touch(self.user.pk, self.device_id)
        await self._set_presence_db(presence.ONLINE)
        await self._broadcast_presence(presence.ONLINE)

        logger.info(
            'connection.established',
            user_id=self.user.pk,
            device_id=self.device_id,
        )

        # Phòng mở sẵn theo URL (giữ tương thích /ws/chat/<id>/).
        # Socket gắn cứng vào một phòng mà không có quyền thì đóng hẳn với
        # close code riêng; socket multiplex chỉ nhận envelope `error`.
        initial = self.scope.get('url_route', {}).get('kwargs', {}).get('conversation_id')
        if initial is not None:
            opened = await self._open_room(int(initial))
            if not opened:
                await self.close(code=CLOSE_FORBIDDEN)

    async def disconnect(self, close_code):
        if not self.user or not self.user.is_authenticated:
            return

        if self.conversation_group:
            await self.channel_layer.group_discard(
                self.conversation_group, self.channel_name,
            )
        await self.channel_layer.group_discard(
            user_group(self.user.pk), self.channel_name,
        )

        # Bỏ marker online của device này; key away còn TTL nên user
        # chuyển sang `away` trước khi thành `offline` (mục 5.7).
        await presence.drop(self.user.pk, self.device_id)
        status = await presence.get_status(self.user.pk) or presence.OFFLINE
        await self._set_presence_db(status)
        await self._broadcast_presence(status)

        logger.info(
            'connection.closed',
            user_id=self.user.pk,
            conversation_id=self.conversation_id,
            close_code=close_code,
        )

    # -----------------------------------------------------------------------
    # Inbound routing
    # -----------------------------------------------------------------------

    async def receive_json(self, content, **kwargs):
        if not isinstance(content, dict):
            await self._error('BAD_ENVELOPE', 'Envelope phải là JSON object.')
            return

        msg_type = content.get('type', '')
        payload = content.get('payload') or {}
        client_message_id = content.get('client_message_id')

        handlers = {
            'room.open': self._on_room_open,
            'message.new': self._on_message_new,
            'message.edited': self._on_message_edit,
            'message.deleted': self._on_message_delete,
            'message.reaction': self._on_message_reaction,
            'message.read': self._on_message_read,
            'message.pinned': self._on_message_pin,
            'message.unpinned': self._on_message_unpin,
            'typing.start': self._on_typing_start,
            'typing.stop': self._on_typing_stop,
            'sync.request': self._on_sync_request,
            'heartbeat.ping': self._on_heartbeat,
        }
        handler = handlers.get(msg_type)
        if handler is None:
            await self._error('UNKNOWN_TYPE', f'Không hỗ trợ type: {msg_type}')
            return
        await handler(payload, client_message_id)

    # -----------------------------------------------------------------------
    # room.open — mục 5.8
    # -----------------------------------------------------------------------

    async def _on_room_open(self, payload, client_message_id=None):
        conversation_id = payload.get('conversation_id')
        if conversation_id is None:
            await self._error('BAD_PAYLOAD', 'Thiếu conversation_id.')
            return
        await self._open_room(int(conversation_id), client_message_id)

    async def _open_room(self, conversation_id, client_message_id=None):
        if not await self._is_member(conversation_id):
            logger.warning(
                'group.join_rejected',
                user_id=self.user.pk,
                conversation_id=conversation_id,
            )
            await self._error(
                'ROOM_FORBIDDEN',
                'Bạn không có quyền vào phòng này.',
                extra={'conversation_id': conversation_id},
            )
            return False

        if self.conversation_group:
            await self.channel_layer.group_discard(
                self.conversation_group, self.channel_name,
            )

        self.conversation_id = conversation_id
        self.conversation_group = Conversation.group_name_for(conversation_id)
        await self.channel_layer.group_add(self.conversation_group, self.channel_name)

        members = await self._member_list(conversation_id)
        await self.send_json(envelope(
            'member.joined',
            {
                'conversation_id': conversation_id,
                'user_id': self.user.pk,
                'username': self.user.username,
                'members': members,
            },
            client_message_id,
        ))
        logger.info(
            'room.opened', user_id=self.user.pk, conversation_id=conversation_id,
        )
        return True

    # -----------------------------------------------------------------------
    # message.new + message.ack
    # -----------------------------------------------------------------------

    async def _on_message_new(self, payload, client_message_id=None):
        conversation_id = payload.get('conversation_id') or self.conversation_id
        if conversation_id is None or int(conversation_id) != self.conversation_id:
            await self._error('ROOM_NOT_OPEN', 'Phòng chưa được mở trên kết nối này.')
            return

        allowed, retry_after = self._check_rate_limit()
        if not allowed:
            logger.warning(
                'message.rate_limited',
                user_id=self.user.pk,
                conversation_id=conversation_id,
            )
            await self._error(
                'RATE_LIMITED',
                f'Gửi quá nhanh, chờ {retry_after}s.',
                extra={'retry_after': retry_after},
                client_message_id=client_message_id,
            )
            return

        text = (payload.get('content') or '').strip()
        cmid = client_message_id or payload.get('client_message_id') or str(uuid.uuid4())

        if not text:
            await self._error('EMPTY_MESSAGE', 'Tin nhắn rỗng.', client_message_id=cmid)
            return
        if len(text) > settings.MESSAGE_MAX_LENGTH:
            logger.warning('message.rejected.too_large', length=len(text))
            await self._error(
                'MESSAGE_TOO_LARGE',
                f'Tin nhắn vượt {settings.MESSAGE_MAX_LENGTH} ký tự.',
                extra={'max_length': settings.MESSAGE_MAX_LENGTH},
                client_message_id=cmid,
            )
            return

        try:
            message, created = await self._create_message(
                conversation_id, text, cmid, payload.get('reply_to_id'),
            )
        except Exception as exc:
            logger.error('message.save_error', error=str(exc))
            await self._error('SAVE_FAILED', 'Không lưu được tin nhắn.', client_message_id=cmid)
            return

        # Ack luôn gửi cho người gửi, kể cả khi trùng client_message_id.
        await self.send_json(envelope('message.ack', {
            'conversation_id': message['conversation_id'],
            'message_id': message['id'],
            'sequence_number': message['sequence_number'],
            'server_time': message['created_at'],
            'duplicate': not created,
        }, cmid))

        if not created:
            # Trùng idempotency key: KHÔNG cấp sequence mới, KHÔNG broadcast lại.
            return

        await safe_group_send(
            self.conversation_group,
            {'type': 'fanout', 'envelope': envelope('message.new', message, cmid)},
            self.channel_layer,
        )
        await self._notify_other_members(message)

        # Xem trước liên kết chạy sau khi đã lưu + broadcast: lỗi hay timeout
        # không bao giờ chặn luồng gửi tin (mục 7.5).
        url = linkpreview.first_url(text)
        if url:
            asyncio.ensure_future(self._attach_preview(message['id'], url))

    # -----------------------------------------------------------------------
    # message.edited / deleted / reaction / read
    # -----------------------------------------------------------------------

    async def _on_message_edit(self, payload, client_message_id=None):
        result, error = await self._edit_message(
            payload.get('message_id'), payload.get('content', ''),
        )
        if error:
            await self._error(error, 'Không sửa được tin nhắn.', client_message_id=client_message_id)
            return
        await safe_group_send(
            self.conversation_group,
            {'type': 'fanout', 'envelope': envelope('message.edited', result)},
            self.channel_layer,
        )

    async def _on_message_delete(self, payload, client_message_id=None):
        result, error = await self._delete_message(payload.get('message_id'))
        if error:
            await self._error(error, 'Không xoá được tin nhắn.', client_message_id=client_message_id)
            return
        await safe_group_send(
            self.conversation_group,
            {'type': 'fanout', 'envelope': envelope('message.deleted', result)},
            self.channel_layer,
        )

    async def _on_message_reaction(self, payload, client_message_id=None):
        result, error = await self._toggle_reaction(
            payload.get('message_id'), payload.get('emoji', ''),
        )
        if error:
            await self._error(error, 'Không đặt được reaction.', client_message_id=client_message_id)
            return
        await safe_group_send(
            self.conversation_group,
            {'type': 'fanout', 'envelope': envelope('message.reaction', result)},
            self.channel_layer,
        )

    async def _on_message_read(self, payload, client_message_id=None):
        if self.conversation_id is None:
            return
        sequence = payload.get('sequence_number')
        if sequence is None:
            return
        applied = await self._mark_read(int(sequence))
        if applied is None:
            return

        # Throttle broadcast 1 lần/giây/phòng (mục 4.4).
        now = time.monotonic()
        last = getattr(self, '_last_read_broadcast', 0.0)
        if now - last < settings.READ_RECEIPT_THROTTLE_SECONDS:
            return
        self._last_read_broadcast = now

        await safe_group_send(
            self.conversation_group,
            {'type': 'fanout', 'envelope': envelope('message.read', {
                'conversation_id': self.conversation_id,
                'user_id': self.user.pk,
                'username': self.user.username,
                'sequence_number': applied,
            })},
            self.channel_layer,
        )

    async def _on_message_pin(self, payload, client_message_id=None):
        await self._set_pinned(payload.get('message_id'), True, client_message_id)

    async def _on_message_unpin(self, payload, client_message_id=None):
        await self._set_pinned(payload.get('message_id'), False, client_message_id)

    async def _set_pinned(self, message_id, pinned, client_message_id):
        result, error = await self._pin_message(message_id, pinned)
        if error:
            await self._error(error, 'Không ghim được tin nhắn.',
                              client_message_id=client_message_id)
            return
        await safe_group_send(
            self.conversation_group,
            {'type': 'fanout', 'envelope': envelope(
                'message.pinned' if pinned else 'message.unpinned', result,
            )},
            self.channel_layer,
        )

    async def _attach_preview(self, message_id, url):
        try:
            preview = await asyncio.to_thread(linkpreview.fetch, url)
            if not preview:
                return
            payload = await self._save_preview(message_id, preview)
            if not payload:
                return
            await safe_group_send(
                self.conversation_group,
                {'type': 'fanout', 'envelope': envelope('message.preview', payload)},
                self.channel_layer,
            )
        except Exception as exc:
            logger.info('link_preview.skipped', error=str(exc))

    # -----------------------------------------------------------------------
    # typing — không chạm DB
    # -----------------------------------------------------------------------

    async def _on_typing_start(self, payload, client_message_id=None):
        await self._typing('typing.start')

    async def _on_typing_stop(self, payload, client_message_id=None):
        await self._typing('typing.stop')

    async def _typing(self, msg_type):
        if not self.conversation_group:
            return
        await safe_group_send(
            self.conversation_group,
            {'type': 'fanout', 'envelope': envelope(msg_type, {
                'conversation_id': self.conversation_id,
                'user_id': self.user.pk,
                'username': self.user.username,
            })},
            self.channel_layer,
        )

    # -----------------------------------------------------------------------
    # sync + heartbeat
    # -----------------------------------------------------------------------

    async def _on_sync_request(self, payload, client_message_id=None):
        conversation_id = payload.get('conversation_id') or self.conversation_id
        if conversation_id is None:
            await self._error('BAD_PAYLOAD', 'Thiếu conversation_id.')
            return
        conversation_id = int(conversation_id)
        if not await self._is_member(conversation_id):
            await self._error('ROOM_FORBIDDEN', 'Bạn không có quyền vào phòng này.')
            return

        after = int(payload.get('after_sequence') or 0)
        messages, has_more = await self._sync_messages(conversation_id, after)
        await self.send_json(envelope('sync.response', {
            'conversation_id': conversation_id,
            'after_sequence': after,
            'messages': messages,
            'has_more': has_more,
        }, client_message_id))

    async def _on_heartbeat(self, payload, client_message_id=None):
        await presence.touch(self.user.pk, self.device_id)
        status = await presence.get_status(self.user.pk) or presence.ONLINE
        if status != presence.ONLINE:
            status = presence.ONLINE  # có heartbeat mới -> về online ngay
        await self._set_presence_db(status)
        await self.send_json(envelope('heartbeat.pong', {
            'server_time': time.time(),
            'status': status,
        }, client_message_id))

    # -----------------------------------------------------------------------
    # Group handlers (channel layer -> socket)
    # -----------------------------------------------------------------------

    async def fanout(self, event):
        """Chuyển tiếp nguyên envelope tới client."""
        await self.send_json(event['envelope'])

    async def fanout_except_sender(self, event):
        if event.get('sender_id') == self.user.pk:
            return
        await self.send_json(event['envelope'])

    async def fanout_cross_room(self, event):
        """
        Bản sao gửi qua kênh riêng `user_<id>` chỉ dành cho phòng KHÔNG mở.
        Socket đang mở đúng phòng đó đã nhận qua group phòng rồi — bỏ qua,
        nếu không client sẽ thấy tin nhắn hai lần.
        """
        payload = event['envelope'].get('payload', {})
        if payload.get('conversation_id') == self.conversation_id:
            return
        await self.send_json(event['envelope'])

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    async def _error(self, code, message, extra=None, client_message_id=None):
        payload = {'code': code, 'message': message}
        if extra:
            payload.update(extra)
        await self.send_json(envelope('error', payload, client_message_id))

    def _check_rate_limit(self):
        """Trả (allowed, retry_after_seconds)."""
        now = time.time()
        window = 1.0
        self._message_timestamps = [
            t for t in self._message_timestamps if now - t < window
        ]
        if len(self._message_timestamps) >= settings.MESSAGE_RATE_LIMIT:
            oldest = min(self._message_timestamps)
            return False, max(1, int(round(window - (now - oldest))) or 1)
        self._message_timestamps.append(now)
        return True, 0

    async def _broadcast_presence(self, status):
        payload = {
            'user_id': self.user.pk,
            'username': self.user.username,
            'status': status,
        }
        for conversation_id in await self._user_conversation_ids():
            await safe_group_send(
                Conversation.group_name_for(conversation_id),
                {
                    'type': 'fanout_except_sender',
                    'sender_id': self.user.pk,
                    'envelope': envelope('presence.update', payload),
                },
                self.channel_layer,
            )

    async def _notify_other_members(self, message):
        """Đẩy message.new lên kênh riêng của member đang không mở phòng này."""
        member_ids = await self._other_member_ids(message['conversation_id'])
        for uid in member_ids:
            await safe_group_send(
                user_group(uid),
                {
                    'type': 'fanout_cross_room',
                    'envelope': envelope('message.new', {**message, 'cross_room': True}),
                },
                self.channel_layer,
            )

    # -----------------------------------------------------------------------
    # DB (mục 5.4 — mọi ORM đều bọc database_sync_to_async)
    # -----------------------------------------------------------------------

    @database_sync_to_async
    def _is_member(self, conversation_id):
        return services.is_member(conversation_id, self.user.pk)

    @database_sync_to_async
    def _member_list(self, conversation_id):
        return services.member_payloads(conversation_id)

    @database_sync_to_async
    def _create_message(self, conversation_id, content, client_message_id, reply_to_id):
        return services.create_message(
            conversation_id, self.user, content, client_message_id, reply_to_id,
        )

    @database_sync_to_async
    def _edit_message(self, message_id, content):
        return services.edit_message(message_id, self.user, content)

    @database_sync_to_async
    def _delete_message(self, message_id):
        return services.delete_message(message_id, self.user)

    @database_sync_to_async
    def _toggle_reaction(self, message_id, emoji):
        return services.toggle_reaction(message_id, self.user, emoji)

    @database_sync_to_async
    def _pin_message(self, message_id, pinned):
        return services.set_pinned(message_id, self.user, pinned)

    @database_sync_to_async
    def _save_preview(self, message_id, preview):
        return services.save_preview(message_id, preview)

    @database_sync_to_async
    def _mark_read(self, sequence_number):
        return services.mark_read(self.conversation_id, self.user, sequence_number)

    @database_sync_to_async
    def _sync_messages(self, conversation_id, after_sequence):
        return services.sync_messages(conversation_id, after_sequence)

    @database_sync_to_async
    def _set_presence_db(self, status):
        PresenceStatus.objects.update_or_create(
            user=self.user, defaults={'status': status},
        )

    @database_sync_to_async
    def _user_conversation_ids(self):
        return list(
            self.user.memberships.values_list('conversation_id', flat=True)
        )

    @database_sync_to_async
    def _other_member_ids(self, conversation_id):
        from .models import ConversationMember
        return list(
            ConversationMember.objects
            .filter(conversation_id=conversation_id)
            .exclude(user=self.user)
            .values_list('user_id', flat=True)
        )


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """
    Kênh thông báo thuần tuý trên `user_<id>` — dành cho client chỉ cần nhận
    thông báo mà không mở phòng nào. Client chính dùng ChatConsumer (đã gồm
    kênh này) nên không mở cả hai để tránh nhận trùng.
    """

    async def connect(self):
        self.user = self.scope.get('user')
        if not self.user or not self.user.is_authenticated:
            await self.close(code=CLOSE_UNAUTHENTICATED)
            return
        subprotocol = (
            WS_SUBPROTOCOL
            if WS_SUBPROTOCOL in (self.scope.get('subprotocols') or [])
            else None
        )
        await self.channel_layer.group_add(user_group(self.user.pk), self.channel_name)
        await self.accept(subprotocol)

    async def disconnect(self, close_code):
        if getattr(self, 'user', None) and self.user.is_authenticated:
            await self.channel_layer.group_discard(
                user_group(self.user.pk), self.channel_name,
            )

    async def fanout(self, event):
        await self.send_json(event['envelope'])

    async def fanout_except_sender(self, event):
        if event.get('sender_id') == getattr(self.user, 'pk', None):
            return
        await self.send_json(event['envelope'])

    async def fanout_cross_room(self, event):
        # Consumer này không mở phòng nào -> luôn chuyển tiếp.
        await self.send_json(event['envelope'])
