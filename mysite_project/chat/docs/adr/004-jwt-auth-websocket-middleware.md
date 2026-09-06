# ADR-004: JWT Authentication cho WebSocket qua Custom AuthMiddleware

## Status

Accepted

## Context

WebSocket connections cần được xác thực trước khi cho phép tham gia vào bất kỳ consumer nghiệp vụ nào. Không cho phép anonymous connections vào consumer chat/notification.

Các phương án xác thực WebSocket:
1. **Session-based** (dùng cookie session của Django)
2. **Token/JWT qua query string** (ws://host/ws/chat/?token=xxx)
3. **Token qua WebSocket subprotocol header**
4. **Token gửi sau khi connect** (first message là auth message)

## Decision

**Dùng JWT token gửi qua query string khi handshake**, xác thực tại custom `TokenAuthMiddleware` TRƯỚC khi request vào consumer. Connection bị reject ngay nếu token không hợp lệ (close code `4001`).

## Rationale

| Tiêu chí | JWT query string | Session cookie | Token first-message |
|----------|-----------------|----------------|---------------------|
| Stateless | ✅ Không cần DB lookup | ❌ Cần session store | ✅ Stateless |
| Auth TRƯỚC connect | ✅ Middleware chặn sớm | ✅ Cookie gửi tự động | ❌ Phải accept rồi mới auth |
| Cross-origin | ✅ Không phụ thuộc cookie | ❌ Cookie SameSite issues | ✅ Không phụ thuộc cookie |
| Mobile client | ✅ Dễ tích hợp | ❌ Cookie quản lý phức tạp | ✅ Dễ tích hợp |
| Security | ⚠️ Token xuất hiện trong URL/log | ✅ Cookie HttpOnly | ⚠️ Window of unauthenticated connection |
| Dễ demo | ✅ Trực quan, dễ test bằng DevTools | ✅ Tự động | ⚠️ Cần thêm logic |

**Vì sao không dùng session cookie?**
- Django sessions mặc định là server-side → thêm DB/cache lookup mỗi lần connect
- WebSocket từ domain khác (nếu có) sẽ không gửi cookie do SameSite policy
- JWT self-contained → verify nhanh, không cần round-trip DB

**Vì sao không dùng token-first-message?**
- Phải accept connection trước → window of unauthenticated connection (dù ngắn)
- Consumer code phức tạp hơn: phải handle 2 trạng thái (chưa auth / đã auth)
- Middleware approach sạch hơn: auth logic tách biệt khỏi business logic

**Mitigation cho token trong URL:**
- Dùng HTTPS/WSS trong production → URL encrypted in transit
- Token có thời hạn ngắn (15-30 phút) + refresh mechanism
- Server log không ghi query string WebSocket (config Daphne)

## Implementation

```python
# chat/middleware.py
class TokenAuthMiddleware:
    """
    Custom middleware that authenticates WebSocket connections
    via JWT token in query string BEFORE reaching the consumer.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        query_string = scope.get("query_string", b"").decode()
        token = parse_qs(query_string).get("token", [None])[0]
        
        if not token:
            await send({"type": "websocket.close", "code": 4001})
            return
            
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
            scope["user"] = await get_user(payload["user_id"])
        except (jwt.InvalidTokenError, User.DoesNotExist):
            await send({"type": "websocket.close", "code": 4001})
            return
            
        return await self.app(scope, receive, send)
```

## Consequences

### Positive
- Auth logic tách biệt trong middleware → consumer chỉ xử lý business logic
- Connection bị reject TRƯỚC khi vào consumer → không lãng phí resource
- Stateless JWT → không cần session store cho WebSocket
- Close code `4001` giúp client phân biệt lỗi auth vs lỗi khác

### Negative
- Token trong query string → cần cẩn thận với logging và access logs
- JWT không revocable ngay lập tức (trừ khi dùng blacklist) → chấp nhận cho demo

### Risks
- Token expire khi connection đang mở → cần handle reconnect + refresh flow ở client

---

## Cập nhật (bản thực thi hiện tại)

Token KHÔNG còn đi qua query string. Client mở kết nối bằng subprotocol:

```js
new WebSocket(url, ["chat.v1", accessToken]);
```

`TokenAuthMiddleware` đọc token từ `scope["subprotocols"]`, consumer
`accept("chat.v1")`. Router còn được bọc thêm `AllowedHostsOriginValidator`
để chặn Origin lạ. Token hết hạn giữa phiên: WS đóng với code 4001, client
refresh token rồi reconnect, thất bại thì quay về màn hình đăng nhập và giữ
lại draft đang gõ.
