"""
WebSocket URL routing.
Mọi endpoint đều qua AllowedHostsOriginValidator + TokenAuthMiddleware.

`ws/chat/` là kết nối chính (một socket cho cả phiên, mở/đổi phòng bằng
message `room.open`). `ws/chat/<id>/` giữ lại để mở sẵn một phòng khi connect.
"""

from django.urls import path

from chat.consumers import ChatConsumer, NotificationConsumer

websocket_urlpatterns = [
    path('ws/chat/', ChatConsumer.as_asgi()),
    path('ws/chat/<int:conversation_id>/', ChatConsumer.as_asgi()),
    path('ws/notifications/', NotificationConsumer.as_asgi()),
]
