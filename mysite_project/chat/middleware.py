"""
WebSocket Authentication Middleware (ADR-004).
Authenticates JWT token from query string BEFORE reaching consumer.
Rejects unauthenticated connections with close code 4001.
Skill: api-security-best-practices
"""

from urllib.parse import parse_qs

import jwt
import structlog
from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser

logger = structlog.get_logger('chat')
User = get_user_model()


@database_sync_to_async
def get_user_from_token(token):
    """Decode JWT and return the user, or None if invalid."""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get('type') != 'access':
            return None
        return User.objects.get(pk=payload['user_id'])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, User.DoesNotExist):
        return None


class TokenAuthMiddleware:
    """
    ASGI middleware that authenticates WebSocket connections via JWT.
    Token is passed as query parameter: ws://host/ws/chat/?token=<jwt>

    If token is missing or invalid, connection is rejected with close code 4001.
    This ensures NO anonymous connections reach business consumers.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'websocket':
            return await self.app(scope, receive, send)

        query_string = scope.get('query_string', b'').decode('utf-8')
        params = parse_qs(query_string)
        token_list = params.get('token', [])

        if not token_list:
            logger.warning('ws.auth.missing_token')
            await send({'type': 'websocket.close', 'code': 4001})
            return

        user = await get_user_from_token(token_list[0])
        if user is None:
            logger.warning('ws.auth.invalid_token')
            await send({'type': 'websocket.close', 'code': 4001})
            return

        scope['user'] = user
        logger.info('ws.auth.success', user_id=user.pk, username=user.username)
        return await self.app(scope, receive, send)


class TokenAuthMiddlewareStack:
    """Convenience wrapper to apply TokenAuthMiddleware to URL routing."""

    def __init__(self, app):
        self.app = TokenAuthMiddleware(app)

    async def __call__(self, scope, receive, send):
        return await self.app(scope, receive, send)
