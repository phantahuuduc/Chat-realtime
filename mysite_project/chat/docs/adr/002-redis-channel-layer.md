# ADR-002: Redis Channel Layer thay vì In-Memory Channel Layer

## Status

Accepted

## Context

Django Channels cung cấp 2 backend Channel Layer:

1. **InMemoryChannelLayer** (`channels.layers.InMemoryChannelLayer`) — chạy trong bộ nhớ của 1 process duy nhất
2. **RedisChannelLayer** (`channels_redis.core.RedisChannelLayer`) — sử dụng Redis Pub/Sub làm message broker giữa các process/worker

Dự án yêu cầu:
- `group_send` broadcast tin nhắn tới tất cả client trong cùng conversation
- Load test 200-500 concurrent WebSocket connections
- Khả năng chạy nhiều ASGI worker (tối thiểu 2) để demo tính mở rộng

## Decision

**Bắt buộc dùng Redis Channel Layer** (`channels_redis`). Cấm dùng InMemoryChannelLayer trong bất kỳ môi trường nào ngoài unit test.

## Rationale

| Tiêu chí | RedisChannelLayer | InMemoryChannelLayer |
|----------|-------------------|----------------------|
| Multi-process support | ✅ Nhiều worker chia sẻ qua Redis | ❌ Chỉ 1 process |
| Load test thực tế | ✅ Đúng kiến trúc production | ❌ Fake — không phản ánh thực tế |
| Persistence qua restart | ✅ Redis giữ state | ❌ Mất hết khi restart |
| group_send reliability | ✅ Qua Pub/Sub, đảm bảo delivery | ⚠️ Chỉ trong cùng process |
| Setup complexity | ⚠️ Cần chạy Redis server | ✅ Zero config |
| Overhead | ⚠️ Network latency tới Redis | ✅ In-memory, cực nhanh |

**Vì sao không chọn InMemoryChannelLayer?**
1. **Load test sẽ sai lệch**: 200 connections trên 1 process → đo hiệu năng của Python GIL, không phải kiến trúc real-time thực tế
2. **Không thể demo multi-worker**: Nếu giảng viên hỏi "chạy thêm 1 worker thì sao?" → InMemory không trả lời được vì message không chia sẻ giữa processes
3. **Tài liệu Django Channels cảnh báo rõ**: InMemoryChannelLayer chỉ dành cho testing, không dùng cho production hay demo thực tế

## Consequences

### Positive
- Load test phản ánh đúng kiến trúc production (messages đi qua network tới Redis rồi fan-out)
- Có thể demo graceful degradation khi Redis chết (Mục 11 của kế hoạch)
- Sẵn sàng scale ra nhiều worker nếu cần demo nâng cao (OPTIONAL)

### Negative
- Cần Docker service cho Redis → tăng complexity setup
- Network latency thêm ~0.5-2ms cho mỗi `group_send` (chấp nhận được)

### Mitigation
- Redis chạy qua Docker Compose → setup 1 lần, tự động
- Graceful degradation: `try/except` quanh `group_send`, log + Sentry alert khi Redis down
