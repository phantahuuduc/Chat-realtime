"""
Comprehensive Test Suite — Real-time Chat Platform.
Skill: test-driven-development (Red-Green-Refactor)

Test layers (Plan Section 9.1):
- Unit Tests: model constraints, business rules, rate limiting
- Integration Tests: REST API auth, conversation CRUD, history pagination
- WebSocket Tests: consumer connect/send/receive/disconnect
- Concurrency Tests: ordering verification
"""

import threading
import uuid
from unittest.mock import patch

from channels.db import database_sync_to_async
from channels.exceptions import ChannelFull
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.db import connection as db_connection
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from chat import linkpreview, services
from chat.broadcast import sync_group_send
from chat.consumers import ChatConsumer
from chat.middleware import _extract_token
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
# Service Layer: idempotency + cấp sequence (mục 5.2, 5.3)
# ===========================================================================

class MessageServiceTest(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user('svc_user', password='pass123')
        self.conv = Conversation.objects.create(name='Svc', type='group')
        ConversationMember.objects.create(
            conversation=self.conv, user=self.user, role='owner',
        )

    def test_sequence_starts_at_one_and_increments(self):
        for expected in (1, 2, 3):
            payload, created = services.create_message(
                self.conv.pk, self.user, f'msg {expected}', str(uuid.uuid4()),
            )
            self.assertTrue(created)
            self.assertEqual(payload['sequence_number'], expected)
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.last_sequence, 3)

    def test_duplicate_client_message_id_returns_existing(self):
        cid = str(uuid.uuid4())
        first, created_first = services.create_message(
            self.conv.pk, self.user, 'Original', cid,
        )
        second, created_second = services.create_message(
            self.conv.pk, self.user, 'Retry', cid,
        )
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first['sequence_number'], second['sequence_number'])
        self.assertEqual(second['content'], 'Original')
        self.assertEqual(Message.objects.filter(conversation=self.conv).count(), 1)
        # Trùng key -> KHÔNG cấp sequence mới.
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.last_sequence, 1)

    def test_reply_and_edit_and_delete(self):
        parent, _ = services.create_message(
            self.conv.pk, self.user, 'Gốc', str(uuid.uuid4()),
        )
        child, _ = services.create_message(
            self.conv.pk, self.user, 'Trả lời', str(uuid.uuid4()),
            reply_to_id=parent['id'],
        )
        self.assertEqual(child['reply_to']['id'], parent['id'])

        edited, error = services.edit_message(child['id'], self.user, 'Đã sửa')
        self.assertIsNone(error)
        self.assertEqual(edited['content'], 'Đã sửa')
        self.assertIsNotNone(edited['edited_at'])

        other = User.objects.create_user('intruder', password='pass123')
        _, error = services.edit_message(child['id'], other, 'Hack')
        self.assertEqual(error, 'FORBIDDEN')

        deleted, error = services.delete_message(child['id'], self.user)
        self.assertIsNone(error)
        self.assertTrue(Message.objects.get(pk=child['id']).is_deleted)

    def test_reaction_toggle(self):
        message, _ = services.create_message(
            self.conv.pk, self.user, 'React me', str(uuid.uuid4()),
        )
        emoji = services.QUICK_REACTIONS[0]

        added, error = services.toggle_reaction(message['id'], self.user, emoji)
        self.assertIsNone(error)
        self.assertEqual(added['action'], 'added')
        self.assertEqual(added['reactions'][0]['count'], 1)

        removed, _ = services.toggle_reaction(message['id'], self.user, emoji)
        self.assertEqual(removed['action'], 'removed')
        self.assertEqual(removed['reactions'], [])

        _, error = services.toggle_reaction(message['id'], self.user, '<script>')
        self.assertEqual(error, 'INVALID_EMOJI')

    def test_sync_limit_and_has_more(self):
        for i in range(1, 6):
            Message.objects.create(
                conversation=self.conv, sender=self.user,
                sequence_number=i, content=f'm{i}',
            )
        with override_settings(OFFLINE_SYNC_LIMIT=3):
            rows, has_more = services.sync_messages(self.conv.pk, 0)
        self.assertEqual([r['sequence_number'] for r in rows], [1, 2, 3])
        self.assertTrue(has_more)

        rows, has_more = services.sync_messages(self.conv.pk, 3)
        self.assertEqual([r['sequence_number'] for r in rows], [4, 5])
        self.assertFalse(has_more)


# ===========================================================================
# Concurrency (mục 5.12) — bắt buộc TransactionTestCase, không rollback-wrap
# ===========================================================================

class SequenceConcurrencyTest(TransactionTestCase):
    """Race condition thật: nhiều thread cùng ghi vào một conversation."""

    def setUp(self):
        self.user = User.objects.create_user('racer', password='pass123')
        self.conv = Conversation.objects.create(name='Race', type='group')
        ConversationMember.objects.create(
            conversation=self.conv, user=self.user, role='owner',
        )

    def _run_parallel(self, jobs):
        errors = []

        def wrapper(job):
            try:
                job()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                db_connection.close()

        threads = [threading.Thread(target=wrapper, args=(j,)) for j in jobs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

    def test_parallel_sends_get_unique_consecutive_sequences(self):
        count = 20
        self._run_parallel([
            (lambda i=i: services.create_message(
                self.conv.pk, self.user, f'race {i}', str(uuid.uuid4()),
            ))
            for i in range(count)
        ])

        sequences = sorted(
            Message.objects.filter(conversation=self.conv)
            .values_list('sequence_number', flat=True)
        )
        self.assertEqual(sequences, list(range(1, count + 1)))
        self.conv.refresh_from_db()
        self.assertEqual(self.conv.last_sequence, count)

    def test_same_client_message_id_in_parallel_creates_one_row(self):
        cid = str(uuid.uuid4())
        self._run_parallel([
            (lambda: services.create_message(self.conv.pk, self.user, 'dup', cid))
            for _ in range(8)
        ])
        self.assertEqual(
            Message.objects.filter(
                conversation=self.conv, client_message_id=cid,
            ).count(),
            1,
        )


# ===========================================================================
# Room management API (mục 4.1)
# ===========================================================================

class RoomManagementAPITest(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user('owner1', password='pass123')
        self.guest = User.objects.create_user('guest1', password='pass123')
        resp = self.client.post('/api/auth/login/', {
            'username': 'owner1', 'password': 'pass123',
        })
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access_token"]}')

    def _guest_client(self):
        client = APIClient()
        resp = client.post('/api/auth/login/', {
            'username': 'guest1', 'password': 'pass123',
        })
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {resp.data["access_token"]}')
        return client

    def test_create_room_generates_join_code(self):
        resp = self.client.post('/api/conversations/', {
            'name': 'Moscow', 'type': 'group', 'visibility': 'public',
            'description': 'Chung kết 2008',
        }, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(resp.data['join_code']), 6)
        self.assertEqual(resp.data['visibility'], 'public')

    def test_join_by_code(self):
        created = self.client.post('/api/conversations/', {
            'name': 'Coded', 'type': 'group',
        }, format='json').data

        guest = self._guest_client()
        resp = guest.post('/api/rooms/join/', {
            'join_code': created['join_code'],
        }, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.data['joined'])
        self.assertTrue(
            ConversationMember.objects.filter(
                conversation_id=created['id'], user=self.guest,
            ).exists()
        )

    def test_join_wrong_code(self):
        guest = self._guest_client()
        resp = guest.post('/api/rooms/join/', {'join_code': 'ZZZZZZ'}, format='json')
        self.assertEqual(resp.status_code, 404)

    def test_public_room_list_and_join_without_code(self):
        created = self.client.post('/api/conversations/', {
            'name': 'Open', 'type': 'group', 'visibility': 'public',
        }, format='json').data

        guest = self._guest_client()
        listing = guest.get('/api/rooms/public/')
        self.assertEqual(listing.status_code, 200)
        self.assertTrue(any(r['id'] == created['id'] for r in listing.data['results']))

        resp = guest.post('/api/rooms/join/', {
            'conversation_id': created['id'],
        }, format='json')
        self.assertEqual(resp.status_code, 200)

    def test_private_room_needs_code(self):
        created = self.client.post('/api/conversations/', {
            'name': 'Closed', 'type': 'group', 'visibility': 'private',
        }, format='json').data
        guest = self._guest_client()
        resp = guest.post('/api/rooms/join/', {
            'conversation_id': created['id'],
        }, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_leave_room(self):
        created = self.client.post('/api/conversations/', {
            'name': 'Leaving', 'type': 'group',
            'member_usernames': ['guest1'],
        }, format='json').data
        guest = self._guest_client()
        resp = guest.post(f'/api/rooms/{created["id"]}/leave/')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            ConversationMember.objects.filter(
                conversation_id=created['id'], user=self.guest,
            ).exists()
        )

    def test_member_list_and_remove_permission(self):
        created = self.client.post('/api/conversations/', {
            'name': 'Members', 'type': 'group',
            'member_usernames': ['guest1'],
        }, format='json').data
        room_id = created['id']

        resp = self.client.get(f'/api/rooms/{room_id}/members/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data['my_role'], 'owner')
        self.assertEqual(len(resp.data['members']), 2)

        # Member thường không được đá ai.
        guest = self._guest_client()
        denied = guest.post(
            f'/api/rooms/{room_id}/members/{self.owner.pk}/remove/',
        )
        self.assertEqual(denied.status_code, 403)

        # Owner đá được member.
        allowed = self.client.post(
            f'/api/rooms/{room_id}/members/{self.guest.pk}/remove/',
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertFalse(
            ConversationMember.objects.filter(
                conversation_id=room_id, user=self.guest,
            ).exists()
        )

    def test_message_search(self):
        created = self.client.post('/api/conversations/', {
            'name': 'Searchable', 'type': 'group',
        }, format='json').data
        conv = Conversation.objects.get(pk=created['id'])
        Message.objects.create(
            conversation=conv, sender=self.owner,
            sequence_number=1, content='Ronaldo ghi bàn phút 26',
        )
        Message.objects.create(
            conversation=conv, sender=self.owner,
            sequence_number=2, content='Terry trượt chân',
        )
        resp = self.client.get(
            f'/api/rooms/{conv.pk}/messages/search/?q=ronaldo',
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.data['results']), 1)
        self.assertIn('Ronaldo', resp.data['results'][0]['content'])

    def test_history_pagination_before_sequence(self):
        conv = Conversation.objects.create(name='Paging', type='group')
        ConversationMember.objects.create(conversation=conv, user=self.owner)
        for i in range(1, 11):
            Message.objects.create(
                conversation=conv, sender=self.owner,
                sequence_number=i, content=f'm{i}',
            )
        resp = self.client.get(
            f'/api/conversations/{conv.pk}/messages/?limit=4',
        )
        self.assertEqual([m['sequence_number'] for m in resp.data['results']],
                         [7, 8, 9, 10])
        self.assertTrue(resp.data['has_more'])

        older = self.client.get(
            f'/api/conversations/{conv.pk}/messages/?limit=4&before_sequence=7',
        )
        self.assertEqual([m['sequence_number'] for m in older.data['results']],
                         [3, 4, 5, 6])
        self.assertTrue(older.data['has_more'])


# ===========================================================================
# WebSocket — envelope { type, payload, client_message_id } (mục 5.11)
# ===========================================================================

@override_settings(CHANNEL_LAYERS=TEST_CHANNEL_LAYERS)
class WebSocketConsumerTest(TransactionTestCase):

    async def _create_user_and_conv(self, username='wsuser'):
        user = await database_sync_to_async(User.objects.create_user)(
            username, password='pass123',
        )
        conv = await database_sync_to_async(Conversation.objects.create)(
            name='WS Test', type='group', created_by=user,
        )
        await database_sync_to_async(ConversationMember.objects.create)(
            conversation=conv, user=user, role='owner',
        )
        return user, conv

    def _communicator(self, user, conv=None):
        communicator = WebsocketCommunicator(ChatConsumer.as_asgi(), '/ws/chat/')
        communicator.scope['user'] = user
        communicator.scope['subprotocols'] = ['chat.v1']
        communicator.scope['url_route'] = {
            'kwargs': ({'conversation_id': conv.pk} if conv else {}),
        }
        return communicator

    async def test_connect_and_open_room(self):
        user, conv = await self._create_user_and_conv()
        communicator = self._communicator(user, conv)

        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'member.joined')
        self.assertEqual(response['payload']['conversation_id'], conv.pk)
        self.assertIn('client_message_id', response)

        await communicator.disconnect()

    async def test_non_member_gets_error_envelope(self):
        user = await database_sync_to_async(User.objects.create_user)(
            'outsider', password='pass123',
        )
        conv = await database_sync_to_async(Conversation.objects.create)(
            name='Private', type='group',
        )
        communicator = self._communicator(user, conv)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)

        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'error')
        self.assertEqual(response['payload']['code'], 'ROOM_FORBIDDEN')

        await communicator.disconnect()

    async def test_send_message_gets_ack_then_broadcast(self):
        user, conv = await self._create_user_and_conv()
        communicator = self._communicator(user, conv)
        await communicator.connect()
        await communicator.receive_json_from(timeout=5)  # member.joined

        cid = str(uuid.uuid4())
        await communicator.send_json_to({
            'type': 'message.new',
            'payload': {'conversation_id': conv.pk, 'content': 'Hello World!'},
            'client_message_id': cid,
        })

        ack = await communicator.receive_json_from(timeout=5)
        self.assertEqual(ack['type'], 'message.ack')
        self.assertEqual(ack['client_message_id'], cid)
        self.assertEqual(ack['payload']['sequence_number'], 1)
        self.assertFalse(ack['payload']['duplicate'])
        self.assertIn('server_time', ack['payload'])

        broadcast = await communicator.receive_json_from(timeout=5)
        self.assertEqual(broadcast['type'], 'message.new')
        self.assertEqual(broadcast['payload']['content'], 'Hello World!')
        self.assertEqual(broadcast['payload']['sequence_number'], 1)

        await communicator.disconnect()

    async def test_idempotent_resend_acks_without_rebroadcast(self):
        user, conv = await self._create_user_and_conv()
        communicator = self._communicator(user, conv)
        await communicator.connect()
        await communicator.receive_json_from(timeout=5)

        cid = str(uuid.uuid4())
        payload = {'conversation_id': conv.pk, 'content': 'Original'}
        await communicator.send_json_to({
            'type': 'message.new', 'payload': payload, 'client_message_id': cid,
        })
        await communicator.receive_json_from(timeout=5)  # ack
        await communicator.receive_json_from(timeout=5)  # message.new

        await communicator.send_json_to({
            'type': 'message.new',
            'payload': {'conversation_id': conv.pk, 'content': 'Retry'},
            'client_message_id': cid,
        })
        ack = await communicator.receive_json_from(timeout=5)
        self.assertEqual(ack['type'], 'message.ack')
        self.assertTrue(ack['payload']['duplicate'])
        self.assertEqual(ack['payload']['sequence_number'], 1)

        # Không có broadcast thứ hai.
        self.assertTrue(await communicator.receive_nothing(timeout=1))

        count = await database_sync_to_async(
            Message.objects.filter(conversation=conv).count
        )()
        self.assertEqual(count, 1)

        await communicator.disconnect()

    async def test_heartbeat_pong(self):
        user, conv = await self._create_user_and_conv()
        communicator = self._communicator(user, conv)
        await communicator.connect()
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({
            'type': 'heartbeat.ping', 'payload': {}, 'client_message_id': None,
        })
        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'heartbeat.pong')
        self.assertIn('server_time', response['payload'])

        await communicator.disconnect()

    async def test_sync_request_returns_missed_messages(self):
        user, conv = await self._create_user_and_conv()
        for i in range(1, 4):
            await database_sync_to_async(Message.objects.create)(
                conversation=conv, sender=user,
                sequence_number=i, content=f'Missed {i}',
            )

        communicator = self._communicator(user, conv)
        await communicator.connect()
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({
            'type': 'sync.request',
            'payload': {'conversation_id': conv.pk, 'after_sequence': 1},
            'client_message_id': None,
        })
        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'sync.response')
        self.assertFalse(response['payload']['has_more'])
        self.assertEqual(
            [m['sequence_number'] for m in response['payload']['messages']], [2, 3],
        )

        await communicator.disconnect()

    async def test_unknown_type_returns_error_envelope(self):
        user, conv = await self._create_user_and_conv()
        communicator = self._communicator(user, conv)
        await communicator.connect()
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({
            'type': 'nonsense.type', 'payload': {}, 'client_message_id': None,
        })
        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'error')
        self.assertEqual(response['payload']['code'], 'UNKNOWN_TYPE')

        await communicator.disconnect()

    async def test_typing_and_reaction_broadcast_between_two_clients(self):
        user_a, conv = await self._create_user_and_conv('typer_a')
        user_b = await database_sync_to_async(User.objects.create_user)(
            'typer_b', password='pass123',
        )
        await database_sync_to_async(ConversationMember.objects.create)(
            conversation=conv, user=user_b, role='member',
        )

        client_a = self._communicator(user_a, conv)
        client_b = self._communicator(user_b, conv)
        await client_a.connect()
        await client_b.connect()
        await client_a.receive_json_from(timeout=5)
        await client_b.receive_json_from(timeout=5)

        await client_a.send_json_to({
            'type': 'typing.start',
            'payload': {'conversation_id': conv.pk},
            'client_message_id': None,
        })
        typing = await client_b.receive_json_from(timeout=5)
        self.assertEqual(typing['type'], 'typing.start')
        self.assertEqual(typing['payload']['username'], 'typer_a')

        await client_a.disconnect()
        await client_b.disconnect()

    async def test_message_too_large_is_rejected(self):
        user, conv = await self._create_user_and_conv()
        communicator = self._communicator(user, conv)
        await communicator.connect()
        await communicator.receive_json_from(timeout=5)

        await communicator.send_json_to({
            'type': 'message.new',
            'payload': {'conversation_id': conv.pk, 'content': 'x' * 6000},
            'client_message_id': str(uuid.uuid4()),
        })
        response = await communicator.receive_json_from(timeout=5)
        self.assertEqual(response['type'], 'error')
        self.assertEqual(response['payload']['code'], 'MESSAGE_TOO_LARGE')

        await communicator.disconnect()


# ===========================================================================
# Middleware & broadcast helpers
# ===========================================================================

class TokenSubprotocolTest(TestCase):
    def test_token_extracted_from_subprotocols(self):
        scope = {'subprotocols': ['chat.v1', 'jwt-token-value']}
        self.assertEqual(_extract_token(scope), 'jwt-token-value')

    def test_missing_subprotocol_returns_none(self):
        self.assertIsNone(_extract_token({'subprotocols': ['jwt-token-value']}))
        self.assertIsNone(_extract_token({'subprotocols': []}))
        self.assertIsNone(_extract_token({}))


class BroadcastResilienceTest(TestCase):
    def test_sync_group_send_swallows_channel_layer_errors(self):
        class Broken:
            async def group_send(self, group, payload):
                raise ChannelFull('queue full')

        with patch('chat.broadcast.get_channel_layer', return_value=Broken()):
            self.assertFalse(sync_group_send('grp', {'type': 'fanout'}))

    def test_sync_group_send_swallows_redis_errors(self):
        class Broken:
            async def group_send(self, group, payload):
                raise ConnectionError('redis down')

        with patch('chat.broadcast.get_channel_layer', return_value=Broken()):
            self.assertFalse(sync_group_send('grp', {'type': 'fanout'}))


# ===========================================================================
# Ghim tin nhắn (mục 7.2)
# ===========================================================================

class PinMessageTest(TransactionTestCase):
    def setUp(self):
        self.owner = User.objects.create_user('pin_owner', password='pass123')
        self.member = User.objects.create_user('pin_member', password='pass123')
        self.conv = Conversation.objects.create(name='Pin', type='group')
        ConversationMember.objects.create(
            conversation=self.conv, user=self.owner, role='owner',
        )
        ConversationMember.objects.create(
            conversation=self.conv, user=self.member, role='member',
        )
        self.message, _ = services.create_message(
            self.conv.pk, self.owner, 'Ghim tôi', str(uuid.uuid4()),
        )

    def test_owner_can_pin_and_unpin(self):
        payload, error = services.set_pinned(self.message['id'], self.owner, True)
        self.assertIsNone(error)
        self.assertTrue(payload['is_pinned'])
        self.assertTrue(Message.objects.get(pk=self.message['id']).is_pinned)

        payload, error = services.set_pinned(self.message['id'], self.owner, False)
        self.assertIsNone(error)
        self.assertFalse(Message.objects.get(pk=self.message['id']).is_pinned)

    def test_member_cannot_pin_other_message(self):
        _, error = services.set_pinned(self.message['id'], self.member, True)
        self.assertEqual(error, 'FORBIDDEN')

    def test_author_can_pin_own_message(self):
        own, _ = services.create_message(
            self.conv.pk, self.member, 'Của tôi', str(uuid.uuid4()),
        )
        _, error = services.set_pinned(own['id'], self.member, True)
        self.assertIsNone(error)

    def test_delete_clears_pin(self):
        services.set_pinned(self.message['id'], self.owner, True)
        services.delete_message(self.message['id'], self.owner)
        self.assertFalse(Message.objects.get(pk=self.message['id']).is_pinned)
        self.assertEqual(services.pinned_messages(self.conv.pk), [])


# ===========================================================================
# Xem trước liên kết (mục 7.5)
# ===========================================================================

class LinkPreviewTest(TestCase):
    def test_first_url_extraction(self):
        self.assertEqual(
            linkpreview.first_url('xem https://example.com/a nhé'),
            'https://example.com/a',
        )
        self.assertIsNone(linkpreview.first_url('không có link nào'))
        self.assertIsNone(linkpreview.first_url(''))

    def test_private_host_is_blocked(self):
        self.assertIsNone(linkpreview.fetch('http://127.0.0.1:8000/'))
        self.assertIsNone(linkpreview.fetch('http://localhost/'))

    def test_non_http_scheme_rejected(self):
        self.assertIsNone(linkpreview.fetch('ftp://example.com/x'))
        self.assertIsNone(linkpreview.fetch('file:///etc/passwd'))

    def test_meta_parsing(self):
        html = (
            '<html><head><title>Fallback</title>'
            '<meta property="og:title" content="Tiêu đề OG">'
            '<meta property="og:description" content="Mô tả">'
            '<meta property="og:image" content="/anh.png">'
            '</head></html>'
        )
        preview = linkpreview._parse_meta(html, 'https://example.com/bai-viet')
        self.assertEqual(preview['title'], 'Tiêu đề OG')
        self.assertEqual(preview['description'], 'Mô tả')
        self.assertEqual(preview['image'], 'https://example.com/anh.png')

    def test_meta_parsing_falls_back_to_title_tag(self):
        preview = linkpreview._parse_meta(
            '<html><head><title>Chỉ có title</title></head></html>',
            'https://example.com/',
        )
        self.assertEqual(preview['title'], 'Chỉ có title')

    def test_message_preview_saved_and_cleared_on_delete(self):
        user = User.objects.create_user('preview_user', password='pass123')
        conv = Conversation.objects.create(name='Preview', type='group')
        ConversationMember.objects.create(conversation=conv, user=user, role='owner')
        message, _ = services.create_message(
            conv.pk, user, 'https://example.com', str(uuid.uuid4()),
        )
        payload = services.save_preview(
            message['id'], {'url': 'https://example.com', 'title': 'Example'},
        )
        self.assertEqual(payload['preview']['title'], 'Example')

        services.delete_message(message['id'], user)
        self.assertIsNone(Message.objects.get(pk=message['id']).preview)
