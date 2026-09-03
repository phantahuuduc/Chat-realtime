# ADR-005: PostgreSQL làm Database chính

## Status

Accepted

## Context

Dự án cần database hỗ trợ:
- Transaction isolation với row-level locking (`SELECT ... FOR UPDATE`) cho message ordering
- Unique constraints phức tạp (composite keys) cho idempotency
- Concurrent write workload (nhiều user gửi tin nhắn đồng thời)
- Full-text search (tìm kiếm tin nhắn — OPTIONAL)

Các lựa chọn:
- **SQLite** — đi kèm Django mặc định
- **PostgreSQL** — RDBMS mạnh nhất cho Django
- **MySQL/MariaDB** — phổ biến, nhẹ hơn PostgreSQL

## Decision

**Dùng PostgreSQL 16** làm database duy nhất, chạy qua Docker Compose.

## Rationale

| Tiêu chí | PostgreSQL | SQLite | MySQL |
|----------|-----------|--------|-------|
| `SELECT FOR UPDATE` | ✅ Row-level locking chính xác | ❌ File-level lock (chặn toàn bộ DB) | ✅ Hỗ trợ |
| Concurrent writes | ✅ MVCC, không block reads | ❌ 1 writer tại 1 thời điểm | ⚠️ Lock escalation |
| Composite UNIQUE | ✅ Đầy đủ | ✅ Hỗ trợ | ✅ Hỗ trợ |
| Django support | ✅ Best supported | ✅ Built-in | ✅ Tốt |
| Docker setup | ⚠️ Cần container | ✅ Zero config | ⚠️ Cần container |
| Full-text search | ✅ Built-in (tsvector) | ❌ Extension (FTS5) | ✅ Hỗ trợ |
| JSON field | ✅ Native JSONB | ⚠️ Text field | ✅ JSON type |

**Vì sao không dùng SQLite?**
1. **Critical**: `SELECT FOR UPDATE` trên SQLite lock **toàn bộ file database** → 200 concurrent connections sẽ chờ hàng nhau tuần tự → load test vô nghĩa
2. SQLite chỉ cho phép 1 writer tại 1 thời điểm → bottleneck nghiêm trọng với concurrent chat
3. Không thể chạy nhiều ASGI worker truy cập cùng DB file đồng thời một cách an toàn

**Vì sao không dùng MySQL?**
- Django + PostgreSQL là combo được hỗ trợ tốt nhất (JSONField, ArrayField, full-text search native)
- PostgreSQL MVCC tốt hơn cho concurrent read/write workload
- Tài liệu Django Channels examples thường dùng PostgreSQL

## Consequences

### Positive
- `SELECT FOR UPDATE` lock chỉ 1 row Conversation → các conversation khác không bị ảnh hưởng
- Load test 200-500 connections phản ánh đúng khả năng concurrent access
- Sẵn sàng cho features nâng cao: full-text search, JSONB fields

### Negative
- Cần Docker container cho PostgreSQL → thêm ~100MB RAM cho demo
- Setup phức tạp hơn SQLite (cần connection string, credentials)

### Migration from current state
- Hiện tại `settings.py` dùng SQLite (`django.db.backends.sqlite3`) → sẽ chuyển sang `django.db.backends.postgresql` + `psycopg2-binary` + Docker Compose
