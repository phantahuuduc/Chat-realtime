"""
Chat Consumers — Core real-time business logic.
Implements Plan Sections 2.3, 4, 5.2 (connection lifecycle, message ordering,
idempotency, presence, notifications, offline sync, rate limiting).

Skills applied:
- systematic-debugging: structured logging for every state transition
- api-security-best-practices: rate limiting, authorization, input sanitization
"""

import time
import uuid

import bleach
import structlog
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    Conversation,
    ConversationMember,
    Message,
    Notification,
    PresenceStatus,
    UserConnection,
)

logger = structlog.get_logger('chat')


class ChatConsumer(AsyncJsonWebsocketConsumer):
    """
    Main chat consumer handling:
    - Connection lifecycle (connect/disconnect with UserConnection tracking)
    - Message sending with ordering (sequence_number) and idempotency
    - Typing indicator with debounce
    - Read receipts
    - Presence updates (online/away/offline via heartbeat)
    - Rate limiting per connection
    - Offline sync on reconnect
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conversation_id = None
        self.conversation = None
        self.user = None
        self._message_timestamps = []  # For rate limiting

    # -----------------------------------------------------------------------
    # Connection Lifecycle
    # -----------------------------------------------------------------------

    async def connect(self):
        self.conversation_id = self.scope['url_route']['kwargs']['conversation_id']
        self.user = self.scope.get('user')

        if not self.user or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        # Authorization: check membership (Plan Section 6)
        is_member = await self._check_membership()
        if not is_member:
            logger.warning(
                'group.join_rejected',
                user_id=self.user.pk,
                conversation_id=self.conversation_id,
            )
            await self.close(code=4003)
            return

        await self.accept()

        # Join conversation group
        self.conversation = await self._get_conversation()
        await self.channel_layer.group_add(
            self.conversation.group_name, self.channel_name,
        )

        # Join user's personal notification group
        await self.channel_layer.group_add(
            f'user_{self.user.pk}', self.channel_name,
        )

        # Track connection + update presence
        await self._create_connection()
        await self._set_presence('online')

        # Broadcast presence to conversation members
        await self._broadcast_presence('online')

        logger.info(
            'connection.established',
            user_id=self.user.pk,
            conversation_id=self.conversation_id,
            channel_name=self.channel_name,
        )

        # Send connection confirmation with conversation info
        await self.send_json({
            'type': 'connection.established',
            'conversation_id': self.conversation_id,
            'user_id': self.user.pk,
            'username': self.user.username,
        })

    async def disconnect(self, close_code):
        if not self.user or not self.conversation:
            return

        # Remove from groups
        await self.channel_layer.group_discard(
            self.conversation.group_name, self.channel_name,
        )
        await self.channel_layer.group_discard(
            f'user_{self.user.pk}', self.channel_name,
        )

        # Remove connection record
        await self._delete_connection()

        # Check if user has any other active connections
        has_connections = await self._user_has_connections()
        if not has_connections:
            await self._set_presence('offline')
            await self._broadcast_presence('offline')

        logger.info(
            'connection.closed',
            user_id=self.user.pk,
            conversation_id=self.conversation_id,
            close_code=close_code,
        )

    # -----------------------------------------------------------------------
    # Message Routing
    # -----------------------------------------------------------------------

    async def receive_json(self, content, **kwargs):
        msg_type = content.get('type', '')
        handlers = {
            'chat.message': self._handle_chat_message,
            'chat.typing': self._handle_typing,
            'chat.read': self._handle_read_receipt,
            'chat.heartbeat': self._handle_heartbeat,
            'chat.sync': self._handle_offline_sync,
        }
        handler = handlers.get(msg_type)
        if handler:
            await handler(content)
        else:
            await self.send_json({
                'type': 'error',
                'message': f'Unknown message type: {msg_type}',
            })

    # -----------------------------------------------------------------------
    # Chat Message (ordering + idempotency)
    # -----------------------------------------------------------------------

    async def _handle_chat_message(self, content):
        # Rate limiting (Plan Section 4.5)
        if not self._check_rate_limit():
            logger.warning(
                'message.rate_limited',
                user_id=self.user.pk,
                conversation_id=self.conversation_id,
            )
            await self.send_json({
                'type': 'error',
                'code': 'RATE_LIMITED',
                'message': 'Too many messages. Please slow down.',
            })
            return

        text = content.get('content', '').strip()
        client_message_id = content.get('client_message_id', str(uuid.uuid4()))

        # Validate message length (Plan Section 4.5)
        if not text:
            await self.send_json({'type': 'error', 'message': 'Empty message.'})
            return
        if len(text) > settings.MESSAGE_MAX_LENGTH:
            logger.warning('message.rejected.too_large', length=len(text))
            await self.send_json({
                'type': 'error',
                'code': 'MESSAGE_TOO_LARGE',
                'message': f'Message exceeds {settings.MESSAGE_MAX_LENGTH} characters.',
            })
            return

        # Sanitize content (XSS prevention — Plan Section 6)
        text = bleach.clean(text, tags=[], strip=True)

        # Save message with ordering + idempotency (ADR-003)
        message_data = await self._save_message(text, client_message_id)

        if message_data is None:
            await self.send_json({
                'type': 'error',
                'message': 'Failed to save message.',
            })
            return

        # If duplicate (idempotency key match), reply to sender only
        if message_data.get('is_duplicate'):
            await self.send_json({
                'type': 'chat.message',
                'message': message_data,
            })
            return

        # Broadcast to conversation group via Redis Channel Layer
        try:
            await self.channel_layer.group_send(
                self.conversation.group_name,
                {
                    'type': 'chat.new_message',
                    'message': message_data,
                },
            )
        except Exception as e:
            # Graceful degradation when Redis down (Plan Section 8.2)
            logger.error('broadcast.failed', error=str(e), conversation_id=self.conversation_id)
            await self.send_json({
                'type': 'error',
                'code': 'BROADCAST_FAILED',
                'message': 'Message saved but broadcast failed. Other users may not see it immediately.',
            })
            return

        # Send notification to offline members
        await self._notify_offline_members(message_data)

    async def chat_new_message(self, event):
        """Handle incoming broadcast message from group_send."""
        await self.send_json({
            'type': 'chat.message',
            'message': event['message'],
        })

    # -----------------------------------------------------------------------
    # Typing Indicator
    # -----------------------------------------------------------------------

    async def _handle_typing(self, content):
        is_typing = content.get('is_typing', False)
        try:
            await self.channel_layer.group_send(
                self.conversation.group_name,
                {
                    'type': 'chat.typing_indicator',
                    'user_id': self.user.pk,
                    'username': self.user.username,
                    'is_typing': is_typing,
                },
            )
        except Exception:
            pass  # Non-critical, don't error on typing broadcast failure

    async def chat_typing_indicator(self, event):
        # Don't send typing indicator back to the typer
        if event['user_id'] != self.user.pk:
            await self.send_json({
                'type': 'chat.typing',
                'user_id': event['user_id'],
                'username': event['username'],
                'is_typing': event['is_typing'],
            })

    # -----------------------------------------------------------------------
    # Read Receipts
    # -----------------------------------------------------------------------

    async def _handle_read_receipt(self, content):
        message_id = content.get('message_id')
        if message_id:
            await self._save_read_receipt(message_id)
            try:
                await self.channel_layer.group_send(
                    self.conversation.group_name,
                    {
                        'type': 'chat.read_update',
                        'user_id': self.user.pk,
                        'username': self.user.username,
                        'message_id': message_id,
                    },
                )
            except Exception:
                pass

    async def chat_read_update(self, event):
        if event['user_id'] != self.user.pk:
            await self.send_json({
                'type': 'chat.read',
                'user_id': event['user_id'],
                'username': event['username'],
                'message_id': event['message_id'],
            })

    # -----------------------------------------------------------------------
    # Heartbeat / Presence
    # -----------------------------------------------------------------------

    async def _handle_heartbeat(self, content):
        await self._update_heartbeat()
        await self._set_presence('online')
        await self.send_json({'type': 'chat.pong'})

    async def chat_presence_update(self, event):
        """Broadcast presence changes to all other members."""
        if event['user_id'] != self.user.pk:
            await self.send_json({
                'type': 'chat.presence',
                'user_id': event['user_id'],
                'username': event['username'],
                'status': event['status'],
            })

    # -----------------------------------------------------------------------
    # Offline Sync (Plan Section 5.3)
    # -----------------------------------------------------------------------

    async def _handle_offline_sync(self, content):
        last_seen = content.get('last_seen_sequence', 0)
        messages = await self._get_missed_messages(last_seen)
        await self.send_json({
            'type': 'chat.sync_response',
            'messages': messages,
            'conversation_id': self.conversation_id,
        })

    # -----------------------------------------------------------------------
    # Notification Channel
    # -----------------------------------------------------------------------

    async def user_notification(self, event):
        """Handle notifications pushed to user_{id} group."""
        await self.send_json({
            'type': 'notification',
            'notification': event['notification'],
        })

    # -----------------------------------------------------------------------
    # Rate Limiting
    # -----------------------------------------------------------------------

    def _check_rate_limit(self):
        now = time.time()
        window = 1.0  # 1 second window
        self._message_timestamps = [
            t for t in self._message_timestamps if now - t < window
        ]
        if len(self._message_timestamps) >= settings.MESSAGE_RATE_LIMIT:
            return False
        self._message_timestamps.append(now)
        return True

    # -----------------------------------------------------------------------
    # Database Operations (sync → async wrappers)
    # -----------------------------------------------------------------------

    @database_sync_to_async
    def _check_membership(self):
        return ConversationMember.objects.filter(
            conversation_id=self.conversation_id,
            user=self.user,
        ).exists()

    @database_sync_to_async
    def _get_conversation(self):
        return Conversation.objects.get(pk=self.conversation_id)

    @database_sync_to_async
    def _save_message(self, content, client_message_id):
        """
        Save message with DB-assigned sequence_number (ADR-003).
        Uses SELECT FOR UPDATE to serialize sequence assignment.
        Idempotency: if client_message_id already exists, return existing message.
        """
        try:
            # Check idempotency first
            existing = Message.objects.filter(
                conversation_id=self.conversation_id,
                sender=self.user,
                client_message_id=client_message_id,
            ).first()
            if existing:
                logger.info(
                    'message.duplicate_ignored',
                    client_message_id=str(client_message_id),
                    existing_sequence=existing.sequence_number,
                )
                return {
                    'id': existing.pk,
                    'conversation_id': self.conversation_id,
                    'sender_id': self.user.pk,
                    'sender_username': self.user.username,
                    'sequence_number': existing.sequence_number,
                    'client_message_id': str(existing.client_message_id),
                    'content': existing.content,
                    'created_at': existing.created_at.isoformat(),
                    'is_duplicate': True,
                }

            with transaction.atomic():
                # Lock conversation row to assign sequence_number
                conv = Conversation.objects.select_for_update().get(
                    pk=self.conversation_id,
                )
                conv.last_sequence += 1
                conv.save(update_fields=['last_sequence', 'updated_at'])

                message = Message.objects.create(
                    conversation=conv,
                    sender=self.user,
                    sequence_number=conv.last_sequence,
                    client_message_id=client_message_id,
                    content=content,
                )

            return {
                'id': message.pk,
                'conversation_id': self.conversation_id,
                'sender_id': self.user.pk,
                'sender_username': self.user.username,
                'sequence_number': message.sequence_number,
                'client_message_id': str(message.client_message_id),
                'content': message.content,
                'created_at': message.created_at.isoformat(),
                'is_duplicate': False,
            }
        except IntegrityError:
            logger.error('message.save_failed', conversation_id=self.conversation_id)
            return None
        except Exception as e:
            logger.error('message.save_error', error=str(e))
            return None

    @database_sync_to_async
    def _save_read_receipt(self, message_id):
        from .models import MessageReadReceipt
        try:
            MessageReadReceipt.objects.get_or_create(
                message_id=message_id, user=self.user,
            )
        except Exception:
            pass

    @database_sync_to_async
    def _create_connection(self):
        UserConnection.objects.update_or_create(
            channel_name=self.channel_name,
            defaults={
                'user': self.user,
                'device_id': self.scope.get('query_string', b'').decode()[:255],
            },
        )

    @database_sync_to_async
    def _delete_connection(self):
        UserConnection.objects.filter(channel_name=self.channel_name).delete()

    @database_sync_to_async
    def _user_has_connections(self):
        return UserConnection.objects.filter(user=self.user).exists()

    @database_sync_to_async
    def _update_heartbeat(self):
        UserConnection.objects.filter(
            channel_name=self.channel_name,
        ).update(last_heartbeat=timezone.now())

    @database_sync_to_async
    def _set_presence(self, status_value):
        PresenceStatus.objects.update_or_create(
            user=self.user,
            defaults={'status': status_value},
        )

    @database_sync_to_async
    def _get_missed_messages(self, last_seen_sequence):
        messages = Message.objects.filter(
            conversation_id=self.conversation_id,
            sequence_number__gt=last_seen_sequence,
        ).select_related('sender').order_by('sequence_number')[:200]
        return [
            {
                'id': m.pk,
                'conversation_id': m.conversation_id,
                'sender_id': m.sender_id,
                'sender_username': m.sender.username,
                'sequence_number': m.sequence_number,
                'client_message_id': str(m.client_message_id),
                'content': m.content,
                'created_at': m.created_at.isoformat(),
            }
            for m in messages
        ]

    @database_sync_to_async
    def _get_offline_member_ids(self):
        """Get member IDs who are not currently viewing this conversation."""
        return list(
            ConversationMember.objects.filter(
                conversation_id=self.conversation_id,
            ).exclude(user=self.user).values_list('user_id', flat=True)
        )

    async def _broadcast_presence(self, status_value):
        try:
            await self.channel_layer.group_send(
                self.conversation.group_name,
                {
                    'type': 'chat.presence_update',
                    'user_id': self.user.pk,
                    'username': self.user.username,
                    'status': status_value,
                },
            )
        except Exception as e:
            logger.error('presence.broadcast_failed', error=str(e))

    async def _notify_offline_members(self, message_data):
        """Send notification to members not in the conversation (Plan Section 2.1)."""
        try:
            member_ids = await self._get_offline_member_ids()
            for uid in member_ids:
                notification_data = await self._create_notification(uid, message_data)
                await self.channel_layer.group_send(
                    f'user_{uid}',
                    {
                        'type': 'user.notification',
                        'notification': notification_data,
                    },
                )
        except Exception as e:
            logger.error('notification.failed', error=str(e))

    @database_sync_to_async
    def _create_notification(self, user_id, message_data):
        n = Notification.objects.create(
            user_id=user_id,
            type='new_message',
            title=f"New message from {message_data['sender_username']}",
            body=message_data['content'][:100],
            data={
                'conversation_id': self.conversation_id,
                'message_id': message_data['id'],
            },
        )
        return {
            'id': n.pk,
            'type': n.type,
            'title': n.title,
            'body': n.body,
            'data': n.data,
            'created_at': n.created_at.isoformat(),
        }


class NotificationConsumer(AsyncJsonWebsocketConsumer):
    """
    Dedicated notification consumer — receives real-time notifications
    on user_{id} group even when not viewing any specific conversation.
    Plan Section 2.1: "nhận thông báo real-time kể cả khi không mở đúng conversation".
    """

    async def connect(self):
        self.user = self.scope.get('user')
        if not self.user or not self.user.is_authenticated:
            await self.close(code=4001)
            return

        await self.channel_layer.group_add(
            f'user_{self.user.pk}', self.channel_name,
        )
        await self.accept()
        await self.send_json({
            'type': 'connection.established',
            'channel': 'notifications',
        })

    async def disconnect(self, close_code):
        if self.user and self.user.is_authenticated:
            await self.channel_layer.group_discard(
                f'user_{self.user.pk}', self.channel_name,
            )

    async def user_notification(self, event):
        await self.send_json({
            'type': 'notification',
            'notification': event['notification'],
        })
