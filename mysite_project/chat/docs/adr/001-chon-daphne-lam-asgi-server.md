# ADR-001: Chọn Daphne làm ASGI Server

## Status

Accepted

## Context

Dự án cần một ASGI server để xử lý cả HTTP và WebSocket connections đồng thời. Các lựa chọn khả thi:

- **Daphne** — reference implementation chính thức của Django Channels project
- **Uvicorn** — ASGI server phổ biến, dựa trên uvloop
- **Hypercorn** — ASGI server hỗ trợ HTTP/2 và HTTP/3

Yêu cầu:
- Hỗ trợ WebSocket protocol đầy đủ (connect, send, receive, disconnect, close codes)
- Tương thích hoàn toàn với Django Channels consumers và Channel Layer
- Tài liệu tốt, cộng đồng hỗ trợ cho ngữ cảnh đồ án / giảng dạy
- Ổn định cho demo trước giảng viên

## Decision

**Chọn Daphne** làm ASGI server duy nhất cho dự án.

## Rationale

| Tiêu chí | Daphne | Uvicorn | Hypercorn |
|----------|--------|---------|-----------|
| Là reference server của Channels | ✅ Chính thức | ❌ Bên thứ 3 | ❌ Bên thứ 3 |
| Tài liệu Django Channels dùng | ✅ Mặc định | ⚠️ Cần config thêm | ⚠️ Cần config thêm |
| WebSocket handling | ✅ Twisted-based, ổn định | ✅ Tốt | ✅ Tốt |
| Dễ giải thích trong demo | ✅ "Server chính thức" | ⚠️ Cần justify | ⚠️ Cần justify |
| Performance (raw throughput) | ⚠️ Thấp hơn Uvicorn | ✅ Nhanh hơn | ✅ Nhanh hơn |

**Vì sao không chọn Uvicorn?**
- Uvicorn nhanh hơn về throughput HTTP, nhưng dự án tập trung vào WebSocket — nơi sự khác biệt hiệu năng không đáng kể ở quy mô 200-500 connections
- Tài liệu chính thức Django Channels dùng Daphne làm ví dụ → dễ tham chiếu khi giảng viên hỏi
- Khi `daphne` nằm trong `INSTALLED_APPS`, `runserver` tự động chuyển sang ASGI mode → tiện cho development

## Consequences

### Positive
- Toàn bộ tài liệu Django Channels áp dụng trực tiếp, không cần adapter
- `python manage.py runserver` tự động dùng Daphne (không cần chạy server riêng khi dev)
- Giảng viên dễ verify vì Daphne là lựa chọn chuẩn trong tài liệu chính thức

### Negative
- Raw HTTP throughput thấp hơn Uvicorn ~15-20% — chấp nhận được vì đây là demo, không phải production scale
- Phụ thuộc Twisted framework — dependency tree lớn hơn Uvicorn

### Risks
- Nếu Daphne có bug cụ thể với Django 6.1 → fallback sang Uvicorn (chỉ cần đổi lệnh khởi động, không ảnh hưởng code)
