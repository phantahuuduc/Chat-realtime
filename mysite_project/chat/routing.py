"""
WebSocket URL routing for the chat platform.
All WebSocket endpoints require JWT authentication via TokenAuthMiddleware.
"""

from django.urls import path

from chat.consumers import ChatConsumer, NotificationConsumer

websocket_urlpatterns = [
    path('ws/chat/<int:conversation_id>/', ChatConsumer.as_asgi()),
    path('ws/notifications/', NotificationConsumer.as_asgi()),
]
