"""
ASGI config — Real-time Team Chat Platform.
ProtocolTypeRouter splits HTTP and WebSocket traffic.
WebSocket connections pass through TokenAuthMiddleware (ADR-004).
"""

import os

import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'mysite.settings')
django.setup()

from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

from chat.middleware import TokenAuthMiddleware
from chat.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    'http': get_asgi_application(),
    'websocket': TokenAuthMiddleware(
        URLRouter(websocket_urlpatterns)
    ),
})
