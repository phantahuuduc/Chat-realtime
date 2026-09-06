"""
Xem trước liên kết (mục 7.5).

Fetch chạy SAU khi message đã lưu và broadcast, nên lỗi hay timeout không
bao giờ chặn luồng gửi tin. Kết quả cache trong Redis 24h.

Chỉ nhận http/https tới host công khai — chặn loopback và dải IP nội bộ để
người dùng không biến máy chủ thành công cụ dò mạng nội bộ (SSRF).
"""

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.request
from html import unescape
from urllib.parse import urlparse, urljoin

import structlog
from django.conf import settings

logger = structlog.get_logger('chat')

URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
META_RE = re.compile(r'<meta\s+[^>]*>', re.IGNORECASE)
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE | re.DOTALL)
ATTR_RE = re.compile(r'(property|name|content)\s*=\s*"(.*?)"', re.IGNORECASE | re.DOTALL)

USER_AGENT = 'Mozilla/5.0 (compatible; CR7GloryChatBot/1.0)'
CACHE_PREFIX = 'linkpreview:'


def first_url(text):
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0).rstrip('.,);]') if match else None


def _is_public_host(hostname):
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast):
            return False
    return True


def _parse_meta(html, url):
    values = {}
    for tag in META_RE.findall(html):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(tag)}
        key = attrs.get('property') or attrs.get('name')
        if key and 'content' in attrs:
            values.setdefault(key.lower(), unescape(attrs['content']).strip())

    title = values.get('og:title') or values.get('twitter:title')
    if not title:
        match = TITLE_RE.search(html)
        if match:
            title = unescape(re.sub(r'\s+', ' ', match.group(1))).strip()

    description = values.get('og:description') or values.get('description') or ''
    image = values.get('og:image') or values.get('twitter:image') or ''
    if image:
        image = urljoin(url, image)

    if not title:
        return None
    return {
        'url': url,
        'title': title[:160],
        'description': description[:240],
        'image': image[:500],
    }


def _redis():
    try:
        import redis
        return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None


def fetch(url):
    """
    Trả dict preview hoặc None. Không bao giờ raise.
    Đây là hàm ĐỒNG BỘ, phải gọi qua thread từ consumer.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        return None
    if not _is_public_host(parsed.hostname):
        logger.info('link_preview.blocked_host', host=parsed.hostname)
        return None

    client = _redis()
    cache_key = f'{CACHE_PREFIX}{url}'
    if client is not None:
        try:
            cached = client.get(cache_key)
            if cached:
                return json.loads(cached) or None
        except Exception:
            client = None

    preview = None
    try:
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(request, timeout=settings.LINK_PREVIEW_TIMEOUT) as resp:
            content_type = resp.headers.get('Content-Type', '')
            if 'html' in content_type:
                raw = resp.read(settings.LINK_PREVIEW_MAX_BYTES)
                charset = resp.headers.get_content_charset() or 'utf-8'
                preview = _parse_meta(raw.decode(charset, errors='replace'), url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.info('link_preview.failed', url=url, error=str(exc))
        preview = None

    if client is not None:
        try:
            client.setex(cache_key, settings.LINK_PREVIEW_CACHE_TTL, json.dumps(preview or {}))
        except Exception:
            pass

    return preview
