"""
Broadcast helpers — mọi lần đẩy sự kiện qua Channel Layer đều đi qua đây.

Hai bảo đảm (mục 5.1 + 5.10 của tài liệu thực thi):
1. `broadcast_on_commit` chỉ gửi SAU khi transaction hiện tại commit thành công,
   nên client không bao giờ thấy sự kiện của một transaction bị rollback.
2. Mọi lỗi channel layer (ChannelFull, Redis down) được nuốt + log có cấu trúc,
   không để exception giết consumer hay request.
"""

import structlog
from asgiref.sync import async_to_sync
from channels.exceptions import ChannelFull
from channels.layers import get_channel_layer
from django.db import transaction

logger = structlog.get_logger('chat')


def _log_failure(group, event_type, exc):
    logger.error(
        'broadcast.failed',
        group=group,
        event_type=event_type,
        error_class=exc.__class__.__name__,
        error=str(exc),
        channel_full=isinstance(exc, ChannelFull),
    )


async def safe_group_send(group, payload, channel_layer=None):
    """
    Async group_send có bọc lỗi. Trả True nếu gửi được, False nếu thất bại.
    Dùng trong consumer (đã ở ngoài transaction).
    """
    layer = channel_layer or get_channel_layer()
    try:
        await layer.group_send(group, payload)
        return True
    except ChannelFull as exc:
        _log_failure(group, payload.get('type'), exc)
    except Exception as exc:  # Redis down, connection reset, ...
        _log_failure(group, payload.get('type'), exc)
    return False


def sync_group_send(group, payload):
    """group_send từ code đồng bộ (REST view, service layer). Không raise."""
    layer = get_channel_layer()
    if layer is None:
        return False
    try:
        async_to_sync(layer.group_send)(group, payload)
        return True
    except ChannelFull as exc:
        _log_failure(group, payload.get('type'), exc)
    except Exception as exc:
        _log_failure(group, payload.get('type'), exc)
    return False


def broadcast_on_commit(group, payload):
    """
    Hoãn group_send tới sau khi transaction hiện tại commit (mục 5.1).
    Ngoài transaction, Django chạy callback ngay lập tức.
    """
    transaction.on_commit(lambda: sync_group_send(group, payload))
