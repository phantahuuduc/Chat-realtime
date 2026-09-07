"""
Trợ lý AI — TÍNH NĂNG CỘNG THÊM.

Nguyên tắc bao trùm: không có gì trong file này được phép làm hỏng chat
người-người. Mọi lỗi (thiếu khoá, hết quota, mạng chết, API đổi định dạng)
đều biến thành một câu trả lời xin lỗi tử tế, không bao giờ raise ra ngoài.

Toàn bộ phần phụ thuộc vào nhà cung cấp nằm trong `_build_request` và
`_extract_text`. Đổi sang nhà cung cấp khác chỉ cần sửa hai hàm đó cộng ba
biến AI_API_URL / AI_MODEL / AI_API_KEY trong settings.
"""

import asyncio
import json

import structlog
from django.conf import settings

logger = structlog.get_logger('chat')

SYSTEM_PROMPT = (
    'Bạn là CR7 AI, trợ lý trong một ứng dụng chat tiếng Việt có chủ đề '
    'chung kết UEFA Champions League Moscow 2008.\n'
    'Trả lời ngắn gọn, đúng trọng tâm, bằng ngôn ngữ người dùng đang dùng '
    '(mặc định tiếng Việt).\n'
    'Bạn giúp được: giải thích khái niệm, viết và sửa đoạn văn, tóm tắt, '
    'dịch, gợi ý ý tưởng, viết code đơn giản, tính toán cơ bản.\n'
    'Dùng markdown khi thật sự cần (danh sách, code). Không bịa thông tin — '
    'không chắc thì nói thẳng là không chắc.'
)

# Thông điệp thay thế khi lớp AI hỏng. Người dùng luôn nhận được một câu
# trả lời, không bao giờ thấy bong bóng trống hay lỗi kỹ thuật.
FALLBACK_MESSAGES = {
    'disabled': 'Trợ lý AI đang tắt. Mọi cuộc trò chuyện với người khác vẫn hoạt động bình thường.',
    'quota': 'Trợ lý AI đã hết lượt dùng hôm nay. Bạn thử lại sau nhé.',
    'auth': 'Trợ lý AI chưa được cấu hình đúng khoá API. Bạn báo quản trị viên giúp nhé.',
    'model': 'Trợ lý AI đang trỏ tới một model không tồn tại. Bạn báo quản trị viên giúp nhé.',
    'busy': 'Trợ lý AI đang quá tải. Bạn thử lại sau một chút nhé.',
    'timeout': 'Trợ lý AI phản hồi lâu quá nên mình dừng lại. Bạn thử hỏi lại nhé.',
    'network': 'Mình không kết nối được tới trợ lý AI lúc này. Bạn thử lại sau nhé.',
    'empty': 'Mình chưa nghĩ ra câu trả lời cho câu này. Bạn thử hỏi cách khác xem sao.',
}


# Lỗi 5xx theo định nghĩa là thoáng qua, nên thử lại đúng một lần rồi thôi.
RETRY_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 1.0


def is_enabled():
    return bool(settings.AI_ASSISTANT_ENABLED)


# ---------------------------------------------------------------------------
# Phần phụ thuộc nhà cung cấp — Google Gemini (REST, SSE)
# ---------------------------------------------------------------------------

def _build_request(history, prompt):
    """Trả (url, headers, body) cho một lượt hỏi có stream."""
    contents = []
    for role, text in history:
        contents.append({
            'role': 'model' if role == 'assistant' else 'user',
            'parts': [{'text': text}],
        })
    contents.append({'role': 'user', 'parts': [{'text': prompt}]})

    url = (
        f'{settings.AI_API_URL}/{settings.AI_MODEL}:streamGenerateContent'
        f'?alt=sse&key={settings.AI_API_KEY}'
    )
    body = {
        'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
        'contents': contents,
        'generationConfig': {'maxOutputTokens': settings.AI_MAX_OUTPUT_TOKENS},
    }
    return url, {'Content-Type': 'application/json'}, body


def _extract_text(payload):
    """Rút phần chữ từ một sự kiện SSE. Định dạng lạ thì trả chuỗi rỗng."""
    try:
        parts = payload['candidates'][0]['content']['parts']
    except (KeyError, IndexError, TypeError):
        return ''
    return ''.join(part.get('text', '') for part in parts if isinstance(part, dict))


def _classify_status(status_code):
    if status_code in (401, 403):
        return 'auth'
    if status_code == 404:
        return 'model'
    if status_code == 429:
        return 'quota'
    if status_code >= 500:
        return 'busy'
    return 'network'


# ---------------------------------------------------------------------------
# Luồng chữ
# ---------------------------------------------------------------------------

def _iter_sse_lines(raw_line):
    """Một dòng SSE -> payload JSON đã parse, hoặc None nếu bỏ qua được."""
    line = raw_line.strip()
    if not line or not line.startswith('data:'):
        return None
    data = line[len('data:'):].strip()
    if not data or data == '[DONE]':
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


async def stream_reply(history, prompt):
    """
    Sinh từng mẩu chữ của câu trả lời.

    Luôn kết thúc êm: mọi lỗi được nuốt và thay bằng một câu xin lỗi, nên
    hàm gọi không cần try/except và không bao giờ bị vỡ luồng.
    """
    if not is_enabled():
        yield FALLBACK_MESSAGES['disabled']
        return

    prompt = (prompt or '').strip()[:settings.AI_MAX_INPUT_CHARS]
    if not prompt:
        yield FALLBACK_MESSAGES['empty']
        return

    try:
        import httpx
    except ImportError:
        logger.error('ai.httpx_missing')
        yield FALLBACK_MESSAGES['network']
        return

    url, headers, body = _build_request(history, prompt)
    produced = False

    try:
        async with httpx.AsyncClient(timeout=settings.AI_REQUEST_TIMEOUT) as client:
            for attempt in range(RETRY_ATTEMPTS):
                async with client.stream('POST', url, headers=headers, json=body) as response:
                    last_try = attempt == RETRY_ATTEMPTS - 1

                    if response.status_code >= 500 and not last_try:
                        await response.aread()
                        logger.info(
                            'ai.busy_retry',
                            status=response.status_code,
                            model=settings.AI_MODEL,
                        )
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        continue

                    if response.status_code != 200:
                        await response.aread()
                        reason = _classify_status(response.status_code)
                        logger.warning(
                            'ai.http_error',
                            status=response.status_code,
                            reason=reason,
                            model=settings.AI_MODEL,
                        )
                        yield FALLBACK_MESSAGES[reason]
                        return

                    async for line in response.aiter_lines():
                        payload = _iter_sse_lines(line)
                        if payload is None:
                            continue
                        chunk = _extract_text(payload)
                        if chunk:
                            produced = True
                            yield chunk
                break

    except httpx.TimeoutException:
        logger.warning('ai.timeout', model=settings.AI_MODEL)
        if not produced:
            yield FALLBACK_MESSAGES['timeout']
        return
    except Exception as exc:
        logger.error('ai.stream_failed', error_class=exc.__class__.__name__, error=str(exc))
        if not produced:
            yield FALLBACK_MESSAGES['network']
        return

    if not produced:
        yield FALLBACK_MESSAGES['empty']
