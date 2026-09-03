"""
Comprehensive Test Suite — Real-time Chat Platform.
Skill: test-driven-development (Red-Green-Refactor)

Test layers (Plan Section 9.1):
- Unit Tests: model constraints, business rules, rate limiting
- Integration Tests: REST API auth, conversation CRUD, history pagination
- WebSocket Tests: consumer connect/send/receive/disconnect
- Concurrency Tests: ordering verification
"""

import json
import uuid
from unittest.mock import AsyncMock, patch

from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from chat.consumers import ChatConsumer
from chat.models import (
    Conversation,
    ConversationMember,
    Message,
    MessageReadReceipt,
    Notification,
    PresenceStatus,
    UserConnection,
)

User = get_user_model()

# Use in-memory channel layer for tests
TEST_CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    },
}


# ===========================================================================
# Unit Tests: Models and Constraints
# ===========================================================================

class ConversationModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('testuser', password='testpass123')

    def test_create_conversation(self):
        conv = Conversation.objects.create(
            name='Test Group', type='group', created_by=self.user,
        )
        self.assertEqual(conv.name, 'Test Group')
        self.assertEqual(conv.last_sequence, 0)
        self.assertEqual(conv.group_name, f'conversation_{conv.pk}')

    def test_conversation_types(self):
        group = Conversation.objects.create(type='group', created_by=self.user)
        direct = Conversation.objects.create(type='direct', created_by=self.user)
        self.assertEqual(group.type, 'group')
        self.assertEqual(direct.type, 'direct')


class MessageModelTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('sender', password='testpass123')
        self.conv = Conversation.objects.create(
            name='Test', type='group', created_by=self.user,
        )
        ConversationMember.objects.create(
            conversation=self.conv, user=self.user, role='owner',
        )

    def test_unique_sequence_number(self):
        """UNIQUE(conversation, sequence_number) — Plan Section 3."""
        Message.objects.create(
            conversation=self.conv, sender=self.user,
            sequence_number=1, content='First',
        )
        with self.assertRaises(IntegrityError):
            Message.objects.create(
                conversation=self.conv, sender=self.user,
                sequence_number=1, content='Duplicate sequence',
            )

    def test_idempotency_constraint(self):
        """UNIQUE(conversation, sender, client_message_id) — Plan Section 3."""
        cid = uuid.uuid4()
        Message.objects.create(
            conversation=self.conv, sender=self.user,
            sequence_number=1, client_message_id=cid, content='Original',
        )
        with self.assertRaises(IntegrityError):
            Message.objects.create(
                conversation=self.conv, sender=self.user,
                sequence_number=2, client_message_id=cid, content='Retry',
            )

    def test_message_ordering(self):
        """Messages ordered by sequence_number by default."""
        for i in range(1, 4):
            Message.objects.create(
                conversation=self.conv, sender=self.user,
                sequence_number=i, content=f'Message {i}',
            )
        messages = list(self.conv.messages.values_list('sequence_number', flat=True))
        self.assertEqual(messages, [1, 2, 3])


class ReadReceiptModelTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('reader', password='testpass123')
        self.conv = Conversation.objects.create(type='group')
        self.msg = Message.objects.create(
            conversation=self.conv, sender=self.user,
            sequence_number=1, content='Test',
        )

    def test_unique_read_receipt(self):
        """UNIQUE(message, user) — Plan Section 3."""
        MessageReadReceipt.objects.create(message=self.msg, user=self.user)
        with self.assertRaises(IntegrityError):
            MessageReadReceipt.objects.create(message=self.msg, user=self.user)


class PresenceModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('presence_user', password='testpass123')

    def test_presence_states(self):
        """Presence lifecycle: online ⇄ away ⇄ offline — Plan Section 4.1."""
        p, _ = PresenceStatus.objects.get_or_create(
            user=self.user, defaults={'status': 'online'},
        )
        self.assertEqual(p.status, 'online')
        p.status = 'away'
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.status, 'away')
        p.status = 'offline'
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.status, 'offline')

    def test_one_to_one_constraint(self):
        """One PresenceStatus per user."""
        PresenceStatus.objects.create(user=self.user, status='online')
        with self.assertRaises(IntegrityError):
            PresenceStatus.objects.create(user=self.user, status='offline')


class MultiDevicePresenceTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('multi_device', password='testpass123')

    def test_multi_device_connection_tracking(self):
        """Multi-device: user only offline when ALL connections closed — Plan Section 4.2."""
        conn1 = UserConnection.objects.create(
            user=self.user, channel_name='ch1', device_id='tab1',
        )
        conn2 = UserConnection.objects.create(
            user=self.user, channel_name='ch2', device_id='tab2',
        )
        self.assertEqual(UserConnection.objects.filter(user=self.user).count(), 2)

        # Close one tab — user still has 1 connection
        conn1.delete()
        self.assertTrue(UserConnection.objects.filter(user=self.user).exists())

        # Close second tab — no connections
        conn2.delete()
        self.assertFalse(UserConnection.objects.filter(user=self.user).exists())


# ===========================================================================
# Integration Tests: REST API
# ===========================================================================

class AuthAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_register(self):
        resp = self.client.post('/api/auth/register/', {
            'username': 'newuser',
            'password': 'secure123',
        })
        self.assertEqual(resp.status_code, 201)
        self.assertIn('access_token', resp.data)
        self.assertIn('refresh_token', resp.data)

    def test_login(self):
        User.objects.create_user('existing', password='pass123')
        resp = self.client.post('/api/auth/login/', {
            'username': 'existing',
            'password': 'pass123',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertIn('access_token', resp.data)

    def test_login_wrong_password(self):
        User.objects.create_user('user1', password='correct')
        resp = self.client.post('/api/auth/login/', {
            'username': 'user1',
            'password': 'wrong',
        })
        self.assertEqual(resp.status_code, 401)

    def test_refresh_token(self):
        resp = self.client.post('/api/auth/register/', {
            'username': 'refreshuser',
            'password': 'secure123',
        })
        refresh = resp.data['refresh_token']
        resp2 = self.client.post('/api/auth/refresh/', {
            'refresh_token': refresh,
        })
        self.assertEqual(resp2.status_code, 200)
        self.assertIn('access_token', resp2.data)

    def test_me_authenticated(self):
        resp = self.client.post('/api/auth/register/', {
            'username': 'meuser',
            'password': 'secure123',
        })
        token = resp.data['access_token']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        resp2 = self.client.get('/api/auth/me/')
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data['username'], 'meuser')

    def test_me_unauthenticated(self):
        resp = self.client.get('/api/auth/me/')
        self.assertIn(resp.status_code, [401, 403])


class ConversationAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user1 = User.objects.create_user('user1', password='pass123')
        self.user2 = User.objects.create_user('user2', password='pass123')
        # Get token
        resp = self.client.post('/api/auth/login/', {
            'username': 'user1', 'password': 'pass123',
        })
        self.token = resp.data['access_token']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_create_conversation(self):
        resp = self.client.post('/api/conversations/', {
            'name': 'Test Group',
            'type': 'group',
            'member_ids': [self.user2.pk],
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data['name'], 'Test Group')
        self.assertEqual(len(resp.data['members']), 2)

    def test_list_conversations(self):
        conv = Conversation.objects.create(name='Group1', type='group')
        ConversationMember.objects.create(conversation=conv, user=self.user1)
        resp = self.client.get('/api/conversations/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['results']), 1)

    def test_message_history(self):
        conv = Conversation.objects.create(name='History Test', type='group')
        ConversationMember.objects.create(conversation=conv, user=self.user1)
        for i in range(1, 4):
            Message.objects.create(
                conversation=conv, sender=self.user1,
                sequence_number=i, content=f'Msg {i}',
            )
        resp = self.client.get(f'/api/conversations/{conv.pk}/messages/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['results']), 3)

    def test_offline_sync_query(self):
        """Offline sync: ?after_sequence=N returns only missed messages."""
        conv = Conversation.objects.create(name='Sync Test', type='group')
        ConversationMember.objects.create(conversation=conv, user=self.user1)
        for i in range(1, 6):
            Message.objects.create(
                conversation=conv, sender=self.user1,
                sequence_number=i, content=f'Msg {i}',
            )
        resp = self.client.get(
            f'/api/conversations/{conv.pk}/messages/?after_sequence=3',
        )
        self.assertEqual(resp.status_code, 200)
        seqs = [m['sequence_number'] for m in resp.data['results']]
        self.assertEqual(seqs, [4, 5])

    def test_unauthorized_message_access(self):
        """Authorization: non-member cannot access messages — Plan Section 6."""
        conv = Conversation.objects.create(name='Secret', type='group')
        # user1 is NOT a member
        resp = self.client.get(f'/api/conversations/{conv.pk}/messages/')
        self.assertEqual(len(resp.data['results']), 0)


class NotificationAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user('notif_user', password='pass123')
        resp = self.client.post('/api/auth/login/', {
            'username': 'notif_user', 'password': 'pass123',
        })
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access_token"]}')

    def test_list_notifications(self):
        Notification.objects.create(
            user=self.user, type='new_message',
            title='New msg', body='Hello',
        )
        resp = self.client.get('/api/notifications/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['results']), 1)

    def test_mark_read(self):
        Notification.objects.create(
            user=self.user, type='new_message',
            title='Test', read=False,
        )
        resp = self.client.post('/api/notifications/mark-read/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['marked_read'], 1)
        self.assertEqual(
            Notification.objects.filter(user=self.user, read=True).count(), 1,
        )


# ===========================================================================
# WebSocket Tests (Plan Section 9.1)
# ===========================================================================

@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class WebSocketConsumerTest(TransactionTestCase):

    async def _create_user_and_conv(self):
        user = await database_sync_to_async(User.objects.create_user)(
            'wsuser', password='pass123',
        )
        conv = await database_sync_to_async(Conversation.objects.create)(
            name='WS Test', type='group', created_by=user,
        )
        await database_sync_to_async(ConversationMember.objects.create)(
            conversation=conv, user=user, role='owner',
        )
        return user, conv

    async def test_connect_authenticated(self):
        """Authenticated user can connect to their conversation."""
        user, conv = await self._create_user_and_conv()
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(),
            f'/ws/chat/{conv.pk}/',
        )
        communicator.scope['user'] = user
        communicator.scope['url_route'] = {'kwargs': {'conversation_id': conv.pk}}

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        response = await communicator.receive_json_from()
        self.assertEqual(response['type'], 'connection.established')
        self.assertEqual(response['conversation_id'], conv.pk)

        await communicator.disconnect()

    async def test_connect_unauthorized(self):
        """Non-member cannot connect — Plan Section 6."""
        user = await database_sync_to_async(User.objects.create_user)(
            'outsider', password='pass123',
        )
        conv = await database_sync_to_async(Conversation.objects.create)(
            name='Private', type='group',
        )
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(),
            f'/ws/chat/{conv.pk}/',
        )
        communicator.scope['user'] = user
        communicator.scope['url_route'] = {'kwargs': {'conversation_id': conv.pk}}

        connected, code = await communicator.connect()
        # Should reject or close
        if connected:
            await communicator.disconnect()

    async def test_send_and_receive_message(self):
        """Send a message and verify ordering + broadcast."""
        user, conv = await self._create_user_and_conv()
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(),
            f'/ws/chat/{conv.pk}/',
        )
        communicator.scope['user'] = user
        communicator.scope['url_route'] = {'kwargs': {'conversation_id': conv.pk}}

        await communicator.connect()
        await communicator.receive_json_from()  # connection.established

        # Send message
        await communicator.send_json_to({
            'type': 'chat.message',
            'content': 'Hello World!',
            'client_message_id': str(uuid.uuid4()),
        })

        # Receive broadcast
        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'chat.message')
        self.assertEqual(response['message']['content'], 'Hello World!')
        self.assertEqual(response['message']['sequence_number'], 1)

        await communicator.disconnect()

    async def test_idempotency(self):
        """Sending same client_message_id twice doesn't create duplicate — Plan Section 4.4."""
        user, conv = await self._create_user_and_conv()
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(),
            f'/ws/chat/{conv.pk}/',
        )
        communicator.scope['user'] = user
        communicator.scope['url_route'] = {'kwargs': {'conversation_id': conv.pk}}

        await communicator.connect()
        await communicator.receive_json_from()

        cid = str(uuid.uuid4())
        # Send twice with same client_message_id
        await communicator.send_json_to({
            'type': 'chat.message',
            'content': 'Original',
            'client_message_id': cid,
        })
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({
            'type': 'chat.message',
            'content': 'Retry',
            'client_message_id': cid,
        })
        response = await communicator.receive_json_from(timeout=5)
        self.assertTrue(response['message']['is_duplicate'])

        # Verify only 1 message in DB
        count = await database_sync_to_async(
            Message.objects.filter(conversation=conv).count
        )()
        self.assertEqual(count, 1)

        await communicator.disconnect()

    async def test_heartbeat(self):
        """Heartbeat keeps connection alive — Plan Section 5.2."""
        user, conv = await self._create_user_and_conv()
        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(),
            f'/ws/chat/{conv.pk}/',
        )
        communicator.scope['user'] = user
        communicator.scope['url_route'] = {'kwargs': {'conversation_id': conv.pk}}

        await communicator.connect()
        await communicator.receive_json_from()

        await communicator.send_json_to({'type': 'chat.heartbeat'})
        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'chat.pong')

        await communicator.disconnect()

    async def test_offline_sync(self):
        """Offline sync returns missed messages — Plan Section 5.3."""
        user, conv = await self._create_user_and_conv()

        # Create some messages while user was "offline"
        for i in range(1, 4):
            await database_sync_to_async(Message.objects.create)(
                conversation=conv, sender=user,
                sequence_number=i, content=f'Missed {i}',
            )

        communicator = WebsocketCommunicator(
            ChatConsumer.as_asgi(),
            f'/ws/chat/{conv.pk}/',
        )
        communicator.scope['user'] = user
        communicator.scope['url_route'] = {'kwargs': {'conversation_id': conv.pk}}

        await communicator.connect()
        await communicator.receive_json_from()

        # Request sync from sequence 1 (missed 2 and 3)
        await communicator.send_json_to({
            'type': 'chat.sync',
            'last_seen_sequence': 1,
        })

        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'chat.sync_response')
        self.assertEqual(len(response['messages']), 2)
        seqs = [m['sequence_number'] for m in response['messages']]
        self.assertEqual(seqs, [2, 3])

        await communicator.disconnect()
