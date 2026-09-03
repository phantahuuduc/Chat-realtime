# 🏆 CR7 Chat — Real-time Team Chat & Notification Platform

> Đồ án Django Channels: Nền tảng chat real-time sử dụng WebSocket, Django Channels, Redis, và PostgreSQL.

![Python](https://img.shields.io/badge/Python-3.12-blue)
![Django](https://img.shields.io/badge/Django-6.1-green)
![Channels](https://img.shields.io/badge/Channels-4.3-orange)
![Redis](https://img.shields.io/badge/Redis-7-red)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-blue)

---

## 📋 Mục lục

- [Kiến trúc](#-kiến-trúc)
- [Tech Stack](#-tech-stack)
- [Tính năng](#-tính-năng)
- [Cài đặt & Chạy](#-cài-đặt--chạy)
- [API Endpoints](#-api-endpoints)
- [WebSocket Protocol](#-websocket-protocol)
- [Testing](#-testing)
- [Architecture Decision Records](#-architecture-decision-records)
- [Cấu trúc thư mục](#-cấu-trúc-thư-mục)

---

## 🏗 Kiến trúc

```
┌─────────────┐     WebSocket (WSS)     ┌──────────────────┐
│   Browser    │ ◄────────────────────►  │  Daphne (ASGI)   │
│   Client     │     HTTP (REST API)     │                  │
└─────────────┘ ◄────────────────────►  │  TokenAuth MW    │
                                         │  ProtocolRouter  │
                                         └────────┬─────────┘
                                                  │
                           ┌──────────────────────┼──────────────────────┐
                           │                      │                      │
                    ┌──────▼──────┐      ┌───────▼───────┐     ┌───────▼───────┐
                    │ ChatConsumer│      │ Notification  │     │  REST API     │
                    │ (per-conv)  │      │ Consumer      │     │  Views (DRF)  │
                    └──────┬──────┘      └───────┬───────┘     └───────┬───────┘
                           │                      │                      │
                    ┌──────▼──────────────────────▼──────────────────────▼───────┐
                    │                    Channel Layer (Redis)                    │
                    │              group_send → fan-out broadcast                │
                    └──────────────────────────┬─────────────────────────────────┘
                                               │
                    ┌──────────────────────────▼─────────────────────────────────┐
                    │                   PostgreSQL 16                             │
                    │  SELECT FOR UPDATE → sequence_number (ADR-003)             │
                    │  UNIQUE constraints → idempotency                          │
                    └────────────────────────────────────────────────────────────┘
```

---

## 🔧 Tech Stack

| Layer | Technology | ADR |
|-------|-----------|-----|
| ASGI Server | **Daphne** (reference impl) | [ADR-001](docs/adr/001-chon-daphne-lam-asgi-server.md) |
| Channel Layer | **Redis** (Pub/Sub fan-out) | [ADR-002](docs/adr/002-redis-channel-layer.md) |
| Message Ordering | **DB-assigned sequence_number** | [ADR-003](docs/adr/003-sequence-number-do-db-cap.md) |
| WS Authentication | **JWT via query string** | [ADR-004](docs/adr/004-jwt-auth-websocket-middleware.md) |
| Database | **PostgreSQL 16** (row-level lock) | [ADR-005](docs/adr/005-postgresql-lam-database-chinh.md) |
| REST API | Django REST Framework | — |
| Logging | structlog (JSON) | — |
| Error Tracking | Sentry SDK | — |
| XSS Prevention | bleach | — |

---

## ✨ Tính năng

### Core Real-time
- ✅ **Chat nhóm & 1-1** qua WebSocket (Django Channels)
- ✅ **Message ordering** — `sequence_number` do DB cấp (SELECT FOR UPDATE)
- ✅ **Idempotency** — `client_message_id` ngăn duplicate trên retry
- ✅ **Offline sync** — Client gửi `last_seen_sequence`, server trả missed messages
- ✅ **Typing indicator** — Realtime "đang gõ..." với animation dots
- ✅ **Read receipts** — Đánh dấu tin nhắn đã đọc
- ✅ **Heartbeat/Presence** — online ⇄ away ⇄ offline lifecycle

### Security (Skill: api-security-best-practices)
- ✅ **JWT Authentication** — Stateless, Bearer token cho REST + query param cho WS
- ✅ **TokenAuthMiddleware** — Chặn connection TRƯỚC khi vào consumer
- ✅ **Membership authorization** — Chỉ member mới join conversation group
- ✅ **Rate limiting** — 10 msg/sec/connection, configurable
- ✅ **Input sanitization** — bleach strip HTML/XSS
- ✅ **Close codes** — 4001 (auth fail), 4003 (not member)

### Notification System
- ✅ **Real-time notifications** qua `user_{id}` group (kể cả khi không mở conversation)
- ✅ **NotificationConsumer** riêng biệt
- ✅ **Toast UI** — Popup notification khi có tin nhắn mới

### UI (Skill: ui-ux-pro-max)
- ✅ **CR7 Champions League background** với rain particle effects
- ✅ **Glassmorphism design** — backdrop-filter blur, gold accent
- ✅ **Typing dots animation** — 3 bouncing dots
- ✅ **Connection status bar** — Connected/Disconnected/Reconnecting
- ✅ **Message animations** — Slide-in effect cho tin nhắn mới
- ✅ **Auto-reconnect** — Tự nối lại sau 3 giây khi mất kết nối

---

## 🚀 Cài đặt & Chạy

### Yêu cầu
- Python 3.12+
- Docker Desktop (cho PostgreSQL + Redis)
- Git

### 1. Clone & Setup

```bash
cd d:\Seminar_Django_ITC\mysite_project

# Tạo virtual environment
python -m venv venv
venv\Scripts\activate

# Cài dependencies
pip install -r requirements.txt
```

### 2. Khởi động PostgreSQL & Redis

```bash
docker-compose up -d db redis
```

### 3. Setup Database

```bash
python manage.py migrate
python manage.py createsuperuser
```

### 4. Chạy Server

```bash
python manage.py runserver
# Daphne tự động chạy vì đã trong INSTALLED_APPS
```

### 5. Mở Browser

- **Chat UI**: http://localhost:8000/
- **Admin**: http://localhost:8000/admin/

---

## 📡 API Endpoints

### Authentication

| Method | Endpoint | Mô tả |
|--------|---------|-------|
| `POST` | `/api/auth/register/` | Đăng ký (username, password) |
| `POST` | `/api/auth/login/` | Đăng nhập → access + refresh token |
| `POST` | `/api/auth/refresh/` | Làm mới access token |
| `GET` | `/api/auth/me/` | Thông tin user hiện tại |

### Chat

| Method | Endpoint | Mô tả |
|--------|---------|-------|
| `GET` | `/api/conversations/` | Danh sách conversations |
| `POST` | `/api/conversations/` | Tạo conversation mới |
| `GET` | `/api/conversations/{id}/` | Chi tiết conversation |
| `GET` | `/api/conversations/{id}/messages/` | Lịch sử tin nhắn |
| `GET` | `/api/conversations/{id}/messages/?after_sequence=N` | Offline sync |
| `GET` | `/api/notifications/` | Danh sách notifications |
| `POST` | `/api/notifications/mark-read/` | Đánh dấu đã đọc |

---

## 🔌 WebSocket Protocol

### Endpoints

| URL | Consumer | Auth |
|-----|----------|------|
| `ws://host/ws/chat/{conv_id}/?token=JWT` | ChatConsumer | Required |
| `ws://host/ws/notifications/?token=JWT` | NotificationConsumer | Required |

### Message Types (Client → Server)

```json
// Gửi tin nhắn
{"type": "chat.message", "content": "Hello!", "client_message_id": "uuid"}

// Typing indicator
{"type": "chat.typing", "is_typing": true}

// Đánh dấu đã đọc
{"type": "chat.read", "message_id": 42}

// Heartbeat (mỗi 15s)
{"type": "chat.heartbeat"}

// Offline sync
{"type": "chat.sync", "last_seen_sequence": 5}
```

### Message Types (Server → Client)

```json
// Tin nhắn mới (broadcast)
{"type": "chat.message", "message": {"id": 1, "sequence_number": 1, "content": "...", ...}}

// Typing
{"type": "chat.typing", "username": "user1", "is_typing": true}

// Presence
{"type": "chat.presence", "username": "user1", "status": "online"}

// Heartbeat response
{"type": "chat.pong"}

// Offline sync response
{"type": "chat.sync_response", "messages": [...]}

// Notification
{"type": "notification", "notification": {"title": "...", "body": "..."}}

// Error
{"type": "error", "code": "RATE_LIMITED", "message": "..."}
```

### Close Codes

| Code | Meaning |
|------|---------|
| 4001 | Authentication failed (missing/invalid JWT) |
| 4003 | Authorization failed (not a member) |

---

## 🧪 Testing

### Unit Tests

```bash
# Chạy tất cả tests (cần SQLite fallback hoặc PostgreSQL)
python manage.py test chat --verbosity=2
```

### Test Coverage

| Layer | Test Cases | Mô tả |
|-------|-----------|-------|
| Model Unit | 7 tests | UNIQUE constraints, ordering, presence states, multi-device |
| REST API | 10 tests | Auth register/login/refresh, CRUD, offline sync, authorization |
| WebSocket | 5 tests | Connect/auth/send/idempotency/heartbeat/offline-sync |

### Load Test (Plan Section 9.2)

```bash
pip install websockets httpx
python scripts/load_test.py --host localhost:8000 --connections 200 --duration 60
```

**Acceptance Criteria:**
- ✅ 200-500 concurrent WebSocket connections
- ✅ 0 messages lost, 0 duplicates
- ✅ p95 fanout latency < 500ms
- ✅ Error rate < 1%

---

## 📄 Architecture Decision Records

| ADR | Quyết định | Câu hỏi giảng viên |
|-----|-----------|---------------------|
| [001](docs/adr/001-chon-daphne-lam-asgi-server.md) | Daphne làm ASGI Server | "Vì sao không dùng Uvicorn?" |
| [002](docs/adr/002-redis-channel-layer.md) | Redis Channel Layer bắt buộc | "Vì sao cần Redis?" |
| [003](docs/adr/003-sequence-number-do-db-cap.md) | Sequence number do DB cấp | "Vì sao không để client đánh số?" |
| [004](docs/adr/004-jwt-auth-websocket-middleware.md) | JWT auth cho WebSocket | "Vì sao không dùng session?" |
| [005](docs/adr/005-postgresql-lam-database-chinh.md) | PostgreSQL thay SQLite | "Vì sao không SQLite?" |

---

## 📁 Cấu trúc thư mục

```
mysite_project/
├── accounts/                   # JWT Authentication
│   ├── authentication.py       # DRF JWT Backend
│   ├── urls.py                 # Auth endpoints
│   └── views.py                # Register/Login/Refresh/Me
├── chat/                       # Core Chat Module
│   ├── admin.py                # Django Admin config
│   ├── consumers.py            # ChatConsumer + NotificationConsumer
│   ├── middleware.py            # TokenAuthMiddleware (ADR-004)
│   ├── models.py               # 7 entities + constraints
│   ├── routing.py              # WebSocket URL patterns
│   ├── serializers.py          # DRF serializers + bleach
│   ├── tests.py                # 22 test cases
│   ├── urls.py                 # REST API endpoints
│   └── views.py                # API views + authorization
├── docs/adr/                   # 5 Architecture Decision Records
├── mysite/                     # Django project config
│   ├── asgi.py                 # ProtocolTypeRouter + AuthMiddleware
│   ├── settings.py             # PostgreSQL + Redis + Channels + JWT
│   └── urls.py                 # Root URL routing
├── scripts/
│   └── load_test.py            # WebSocket load test (p50/p95/p99)
├── static/
│   └── cr7.jpg                 # CR7 Champions League background
├── templates/
│   └── chat.html               # Premium chat UI (glassmorphism + rain)
├── .env                        # Environment variables (local)
├── .env.example                # Template for .env
├── docker-compose.yml          # PostgreSQL 16 + Redis 7
├── Dockerfile                  # Python 3.12-slim
├── requirements.txt            # Pinned dependencies
└── manage.py                   # Django management
```

---

## 🎯 Data Model (ERD)

```
┌─────────────────┐     1:N     ┌─────────────────────┐
│  User (Django)  │ ──────────► │ ConversationMember   │
│                 │             │ (role: owner/admin/   │
│                 │             │  member)              │
└────────┬────────┘             └──────────┬────────────┘
         │                                 │
         │ 1:N                             │ N:1
         │                                 │
         │       ┌─────────────────┐       │
         │       │  Conversation   │ ◄─────┘
         │       │  (type, name,   │
         │       │   last_sequence)│
         │       └───────┬─────────┘
         │               │ 1:N
         │               ▼
         │       ┌─────────────────┐
         ├─────► │    Message      │
         │ 1:N   │ (sequence_num,  │
         │       │  client_msg_id, │
         │       │  content)       │
         │       └───────┬─────────┘
         │               │ 1:N
         │               ▼
         │       ┌─────────────────┐
         ├─────► │ MessageRead     │
         │       │ Receipt         │
         │       └─────────────────┘
         │
         │ 1:1   ┌─────────────────┐
         ├─────► │ PresenceStatus  │
         │       │ (online/away/   │
         │       │  offline)       │
         │       └─────────────────┘
         │
         │ 1:N   ┌─────────────────┐
         ├─────► │ UserConnection  │
         │       │ (channel_name,  │
         │       │  device_id,     │
         │       │  heartbeat)     │
         │       └─────────────────┘
         │
         │ 1:N   ┌─────────────────┐
         └─────► │ Notification    │
                 │ (type, title,   │
                 │  body, data,    │
                 │  read)          │
                 └─────────────────┘

Constraints:
  UNIQUE(conversation, sequence_number)         — ordering integrity
  UNIQUE(conversation, sender, client_msg_id)   — idempotency
  UNIQUE(message, user)                         — no duplicate read receipts
  UNIQUE(channel_name)                          — no duplicate connections
  INDEX(conversation, sequence_number)          — offline sync queries
  INDEX(user, last_heartbeat)                   — presence timeout scan
  INDEX(user, read, -created_at)                — notification queries
```

---

## 🎨 Skills Applied

| Skill | Where Applied |
|-------|--------------|
| `architecture-decision-records` | 5 ADRs in `docs/adr/` |
| `database-design` | Models, constraints, indexes |
| `api-security-best-practices` | JWT auth, rate limiting, sanitization, authorization |
| `test-driven-development` | 22 test cases across 3 layers |
| `performance-optimizer` | Load test script, connection pooling, SELECT FOR UPDATE |
| `systematic-debugging` | structlog structured logging, Sentry integration |
| `ui-ux-pro-max` | CR7 glassmorphism UI, rain effects, micro-animations |

---

## 📝 License

This project is for educational purposes (Đồ án Seminar Django — ITC).
