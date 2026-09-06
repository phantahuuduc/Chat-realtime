"""
WebSocket Authentication Middleware (ADR-004 + mục 5.6).

Token đi qua Sec-WebSocket-Protocol, KHÔNG qua query string:
    new WebSocket(url, ["chat.v1", token])
=> scope['subprotocols'] == ['chat.v1', '<jwt>']

Query string bị loại bỏ vì token lộ trong access log, Referer và lịch sử proxy.
Kết nối thiếu/sai token bị đóng với close code 4001 trước khi tới consumer.
"""

import jwt
import structlog
from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth import get_user_model

logger = structlog.get_logger('chat')
User = get_user_model()

WS_SUBPROTOCOL = 'chat.v1'


@database_sync_to_async
def get_user_from_token(token):
    """Decode JWT và trả về user, None nếu token không hợp lệ."""
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM],
        )
        if payload.get('type') != 'access':
            return None
        return User.objects.get(pk=payload['user_id'])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, User.DoesNotExist):
        return None


def _extract_token(scope):
    """
    Lấy token từ subprotocols. Quy ước: phần tử đầu là 'chat.v1',
    phần tử thứ hai là access token.
    """
    subprotocols = [str(p).strip() for p in scope.get('subprotocols') or []]
    if WS_SUBPROTOCOL not in subprotocols:
        return None
    for value in subprotocols:
        if value != WS_SUBPROTOCOL and value:
            return value
    return None


class TokenAuthMiddleware:
    """
    ASGI middleware xác thực WebSocket bằng JWT lấy từ subprotocol.
    Không có kết nối ẩn danh nào chạm tới consumer nghiệp vụ.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'websocket':
            return await self.app(scope, receive, send)

        token = _extract_token(scope)
        if not token:
            logger.warning('ws.auth.missing_token')
            await send({'type': 'websocket.close', 'code': 4001})
            return

        user = await get_user_from_token(token)
        if user is None:
            logger.warning('ws.auth.invalid_token')
            await send({'type': 'websocket.close', 'code': 4001})
            return

        scope['user'] = user
        logger.info('ws.auth.success', user_id=user.pk, username=user.username)
        return await self.app(scope, receive, send)


class TokenAuthMiddlewareStack:
    """Wrapper tiện dụng để bọc URLRouter."""

    def __init__(self, app):
        self.app = TokenAuthMiddleware(app)

    async def __call__(self, scope, receive, send):
        return await self.app(scope, receive, send)
