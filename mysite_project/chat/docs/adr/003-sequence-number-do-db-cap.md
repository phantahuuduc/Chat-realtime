# ADR-003: Sequence Number do DB cấp, không phải Client

## Status

Accepted

## Context

Trong hệ thống chat real-time, thứ tự tin nhắn (message ordering) là yêu cầu bắt buộc. Khi nhiều client gửi tin nhắn đồng thời vào cùng một conversation, cần có cơ chế đảm bảo:

1. Mỗi tin nhắn có một số thứ tự duy nhất trong conversation
2. Không có 2 tin nhắn trùng số thứ tự
3. Thứ tự nhất quán giữa tất cả client (không phụ thuộc thời điểm nhận qua mạng)

Các lựa chọn:
- **Client sinh sequence_number** (timestamp hoặc counter phía client)
- **Server/DB cấp phát sequence_number** trong transaction

## Decision

**Sequence number do DB cấp phát trong transaction**, sử dụng `SELECT ... FOR UPDATE` trên `Conversation.last_sequence` rồi tăng dần. Client không được tự đánh số.

## Rationale

| Tiêu chí | DB cấp (trong transaction) | Client tự sinh |
|----------|---------------------------|-----------------|
| Tính nhất quán (consistency) | ✅ Single source of truth | ❌ Clock skew giữa các client |
| Race condition | ✅ `SELECT FOR UPDATE` serialize access | ❌ 2 client có thể sinh cùng số |
| Offline client | ✅ Sequence chỉ cấp khi message thực sự lưu | ❌ Client offline có thể sinh số nhưng không gửi được |
| Idempotency kết hợp | ✅ Hoạt động tốt với `client_message_id` | ⚠️ Phức tạp khi kết hợp |
| Performance | ⚠️ Lock ngắn trên row Conversation | ✅ Không lock DB |

**Vì sao không để client tự đánh số?**
1. **Clock skew**: Client A có đồng hồ nhanh hơn Client B 5 giây → tin nhắn của A luôn có timestamp lớn hơn dù B gửi trước
2. **Offline gap**: Client sinh số 42, mất mạng, số 42 không bao giờ tới server → lỗ hổng trong sequence
3. **Malicious client**: Client cố tình gửi `sequence_number = 999999` → phá vỡ ordering cho tất cả client khác
4. **Multi-device**: User mở 2 tab, cả 2 tab sinh sequence cục bộ → trùng lặp hoặc không đồng bộ

## Implementation

```python
# Trong ChatConsumer.receive() — cấp sequence_number trong transaction
from django.db import transaction

with transaction.atomic():
    conversation = Conversation.objects.select_for_update().get(id=conversation_id)
    conversation.last_sequence += 1
    conversation.save(update_fields=['last_sequence'])
    
    message = Message.objects.create(
        conversation=conversation,
        sender=user,
        sequence_number=conversation.last_sequence,
        client_message_id=client_message_id,
        content=content,
    )
```

## Consequences

### Positive
- Thứ tự tin nhắn đảm bảo chính xác 100% — single source of truth tại DB
- Client chỉ cần hiển thị theo `sequence_number`, không cần logic sắp xếp phức tạp
- Concurrency test có thể verify dễ dàng: kiểm tra `UNIQUE(conversation_id, sequence_number)` không vi phạm

### Negative
- `SELECT FOR UPDATE` tạo lock ngắn trên row Conversation → bottleneck nếu hàng nghìn message/giây vào cùng 1 conversation
- Ở quy mô demo (200-500 connections), bottleneck này không đáng kể

### Risks
- Lock contention cao khi nhiều user gửi cùng lúc vào 1 conversation → mitigation: lock chỉ trên row, rất ngắn (microseconds)

---

## Cập nhật (bản thực thi hiện tại)

`SELECT FOR UPDATE` + `UPDATE` đã được thay bằng MỘT câu lệnh atomic, đặt ở cuối
transaction sau mọi validation (`chat/services.py::_next_sequence`):

```sql
UPDATE chat_conversation SET last_sequence = last_sequence + 1, updated_at = %s
WHERE id = %s RETURNING last_sequence
```

Idempotency được kiểm TRƯỚC khi cấp sequence: trùng `client_message_id` thì trả
bản ghi cũ, không cấp sequence mới, không broadcast lại. Kiểm chứng bằng
`SequenceConcurrencyTest` (`TransactionTestCase`, 20 thread song song).
