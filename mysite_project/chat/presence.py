"""
Presence bằng Redis TTL (mục 5.7).

Không có job quét `last_heartbeat`. Mỗi heartbeat ghi 2 key có TTL:

    SETEX presence:{uid}:{did}       PRESENCE_AWAY_TIMEOUT      1   # còn sống = online
    SETEX presence:away:{uid}:{did}  PRESENCE_OFFLINE_TIMEOUT   1   # còn sống = away

Trạng thái của user:
    còn key online nào   -> online
    chỉ còn key away nào -> away
    hết sạch             -> offline

`presence:devices:{uid}` là SET index các device_id để tra cứu không cần SCAN.
Redis hỏng -> fallback về PresenceStatus trong DB, log lỗi, không raise.
"""

import structlog
from django.conf import settings

logger = structlog.get_logger('chat')

ONLINE = 'online'
AWAY = 'away'
OFFLINE = 'offline'

_KEY_ONLINE = 'presence:{uid}:{did}'
_KEY_AWAY = 'presence:away:{uid}:{did}'
_KEY_DEVICES = 'presence:devices:{uid}'


def _client():
    """Redis client async dùng chung REDIS_URL với channel layer."""
    try:
        import redis.asyncio as aioredis
    except ImportError:  # pragma: no cover
        return None
    url = getattr(settings, 'REDIS_URL', None) or 'redis://localhost:6379/0'
    try:
        return aioredis.from_url(url, decode_responses=True)
    except Exception as exc:  # pragma: no cover
        logger.error('presence.redis_unavailable', error=str(exc))
        return None


async def touch(user_id, device_id):
    """Ghi nhận một heartbeat/connect. Trả True nếu Redis nhận."""
    client = _client()
    if client is None:
        return False
    try:
        async with client.pipeline(transaction=False) as pipe:
            pipe.setex(
                _KEY_ONLINE.format(uid=user_id, did=device_id),
                settings.PRESENCE_AWAY_TIMEOUT, 1,
            )
            pipe.setex(
                _KEY_AWAY.format(uid=user_id, did=device_id),
                settings.PRESENCE_OFFLINE_TIMEOUT, 1,
            )
            pipe.sadd(_KEY_DEVICES.format(uid=user_id), device_id)
            pipe.expire(
                _KEY_DEVICES.format(uid=user_id), settings.PRESENCE_OFFLINE_TIMEOUT,
            )
            await pipe.execute()
        return True
    except Exception as exc:
        logger.error('presence.touch_failed', user_id=user_id, error=str(exc))
        return False
    finally:
        await _close(client)


async def drop(user_id, device_id):
    """
    Xoá marker online của một device khi socket đóng chủ động.
    Key away vẫn giữ TTL để user còn ở trạng thái `away` trong thời gian ân hạn.
    """
    client = _client()
    if client is None:
        return False
    try:
        await client.delete(_KEY_ONLINE.format(uid=user_id, did=device_id))
        return True
    except Exception as exc:
        logger.error('presence.drop_failed', user_id=user_id, error=str(exc))
        return False
    finally:
        await _close(client)


async def get_status(user_id):
    """Trả 'online' | 'away' | 'offline'. None nếu không đọc được Redis."""
    client = _client()
    if client is None:
        return None
    try:
        devices = await client.smembers(_KEY_DEVICES.format(uid=user_id))
        if not devices:
            return OFFLINE
        devices = list(devices)
        online_keys = [_KEY_ONLINE.format(uid=user_id, did=d) for d in devices]
        away_keys = [_KEY_AWAY.format(uid=user_id, did=d) for d in devices]
        online_vals = await client.mget(online_keys)
        if any(v is not None for v in online_vals):
            return ONLINE
        away_vals = await client.mget(away_keys)
        if any(v is not None for v in away_vals):
            return AWAY
        # Hết sạch key -> dọn index cho gọn.
        await client.delete(_KEY_DEVICES.format(uid=user_id))
        return OFFLINE
    except Exception as exc:
        logger.error('presence.read_failed', user_id=user_id, error=str(exc))
        return None
    finally:
        await _close(client)


async def get_statuses(user_ids):
    """Trả dict {user_id: status} cho nhiều user."""
    return {uid: (await get_status(uid)) or OFFLINE for uid in user_ids}


async def _close(client):
    try:
        await client.aclose()
    except Exception:  # pragma: no cover
        pass
