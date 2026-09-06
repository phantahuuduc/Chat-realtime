"""
ASGI config — Real-time Team Chat Platform.
ProtocolTypeRouter tách HTTP và WebSocket.

WebSocket đi qua 2 lớp:
1. AllowedHostsOriginValidator — chặn Origin lạ (mục 5.5).
2. TokenAuthMiddleware — xác thực JWT lấy từ Sec-WebSocket-Protocol (ADR-004, mục 5.6).
"""

import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

from chat.middleware import TokenAuthMiddleware
from chat.routing import websocket_urlpatterns

django_asgi_app = get_asgi_application()

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    'websocket': AllowedHostsOriginValidator(
        TokenAuthMiddleware(
            URLRouter(websocket_urlpatterns)
        )
    ),
})
