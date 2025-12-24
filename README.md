# AgentMail Proxy

A high-performance email proxy for AI agents with connection pooling, Redis session persistence, and LLM-ready email transformation.

## Quick Start

### Option 1: Docker Compose (Recommended)

```bash
# Configure
cp .env.example .env
# Edit .env with your Gmail credentials

# Start all services
docker-compose up --build

# Health check
curl http://localhost:8000/health
```

### Option 2: Local Development

```bash
# Install
pip install -e .

# Configure
cp .env.example .env

# Start Redis
docker run --name agentmail-redis -p 6379:6379 -d redis

# Start the proxy
uvicorn src.v3_proxy_api:app --host 0.0.0.0 --port 8000 --reload

# Run tests
pytest tests/ -v
```

---

## Architecture Overview

| Version | Type | Latency | Persistence | Best For |
|---------|------|---------|-------------|----------|
| **v1** | Stateless | ~1200ms | None | Simple scripts |
| **v2** | Memory Pool | ~334ms | Process lifetime | Single-process apps |
| **v3** | Redis Pool | ~304ms | Survives restarts | Production |

---

## Benchmark Results

### Latency Comparison (Warm Requests)

| Version | Latency | Speedup |
|---------|---------|---------|
| v1 Stateless | 1,240ms | - |
| v2 Memory Pool | 334ms | **3.7x** faster |
| v3 Redis Pool | 304ms | **4.1x** faster |

### Cold vs Warm Performance

| Metric | Result |
|--------|--------|
| Cold Start | 1,239ms (includes connect + login) |
| Warm Request | 448ms (connection reused) |
| Session Reuse Rate | **95%** (19/20 requests reuse) |
| Memory Growth | **0%** (stable over 50 requests) |

### Application-Layer Breakdown

| Phase | IMAP | SMTP |
|-------|------|------|
| Connect (TCP+TLS) | 17ms | 20ms |
| Login | 257ms | 146ms |
| Operation | 481ms | 1070ms |
| Disconnect | 96ms | 1ms |
| **Overhead %** | **43%** | **13%** |

---

## Phase 1: Stateless Handlers (Baseline)

Naive handlers that create fresh connections for every operation.

**Key Finding**: IMAP 3x calls waste **72%** of time on connection overhead!

---

## Phase 2: In-Memory Connection Pool

Maintains persistent connections within a process.

| Metric | v1 Stateless | v2 Pooled | Improvement |
|--------|--------------|-----------|-------------|
| IMAP call | 1225ms | 310ms | **4.0x faster** |
| 3 IMAP calls | 2929ms | 944ms | **3.1x faster** |

**Limitation**: Connections lost on application restart.

---

## Phase 3: Redis-Backed Pool

Session metadata persists in Redis, surviving restarts. Available for both IMAP and SMTP.

### Features
- Session persistence across restarts
- TTL-based session expiry with refresh
- Connection reuse within process
- Hit/miss statistics tracking
- Both IMAP (`RedisSMTPPool`) and SMTP (`RedisSMTPPool`) supported

### IMAP Results

| Metric | v1 | v2 Memory | v3 Redis |
|--------|-----|-----------|----------|
| Latency | 1915ms | 541ms | 774ms |
| Improvement | - | 3.5x | 2.5x |
| Persistence | No | No | Yes |

### SMTP Redis Pool

```python
from src.v3_smtp_redis_pool import RedisSMTPPool, HybridSMTPHandler

pool = RedisSMTPPool("redis://localhost:6379/0")
handler = HybridSMTPHandler(pool, {
    "host": "smtp.gmail.com",
    "port": 587,
    "user": "you@gmail.com",
    "password": "app-password",
})

result = await handler.send_message(
    to="recipient@example.com",
    subject="Hello",
    body="Message body",
)
```

---

## Phase 3: RAG Transformer

Transforms raw emails into LLM-ready format.

### Features
- Removes HTML signatures and boilerplate
- Collapses deeply nested quotes (3+ levels)
- Strips tracking pixels (1x1 images)
- Extracts text from PDF attachments
- Truncates to 5000 chars for LLM context

### Test Results

| Test | Result |
|------|--------|
| Signature removal | PASS - Removes `div.signature` |
| Quote collapsing | PASS - `>>>` becomes `[Quoted text collapsed]` |
| Tracking pixel removal | PASS - Strips 1x1 images |
| PDF text extraction | PASS - Extracts via mock/OCR |
| Size reduction | PASS - 50%+ reduction achieved |

---

## Phase 3: Proxy API (SDK Compatible)

FastAPI-based proxy that mimics AgentMail's official SDK interface.

### Features
- Drop-in replacement for AgentMail SDK
- Same method signatures: `messages.list()`, `messages.send()`, `inboxes.create()`
- Pydantic models match official schema
- Connects to legacy IMAP/SMTP under the hood

### Usage

```python
# Official AgentMail SDK
from agentmail import Client
client = Client(api_key="...", base_url="https://api.agentmail.to")

# Drop-in Proxy replacement
from src.v3_proxy_api import ProxyClient
client = ProxyClient(api_key="...", base_url="http://localhost:8000")

# Same method calls work!
messages = await client.messages.list(inbox_id="user@example.com")
```

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/inboxes/{inbox_id}/messages` | List messages |
| POST | `/v1/inboxes/{inbox_id}/messages` | Send message |
| POST | `/v1/inboxes` | Create inbox mapping |
| GET | `/v1/inboxes/{inbox_id}` | Get inbox details |
| DELETE | `/v1/inboxes/{inbox_id}` | Delete inbox |
| GET | `/health` | Health check |

---

## Session Worker

Background worker that keeps IMAP sessions alive.

### Features
- Runs every 25 seconds
- Scans Redis for active sessions
- Refreshes TTL to 300 seconds
- Checks OAuth token expiry (<60s warning)
- Structured JSON logging with structlog
- Privacy-safe: logs user hashes, not emails

### Run

```bash
python -m src.session_worker
```

### Output

```json
{"redis_host": "localhost:6379", "noop_interval": 25, "event": "worker_starting"}
{"count": 2, "event": "sessions_found"}
{"user_hash": "abc123def456", "old_ttl": 180, "ttl": 300, "event": "noop_sent"}
{"total": 2, "success": 2, "failed": 0, "event": "noop_cycle_complete"}
```

---

## Docker Deployment

### Services

| Service | Port | Description |
|---------|------|-------------|
| `proxy` | 8000 | FastAPI proxy API |
| `redis` | 6379 | Session storage |
| `worker` | - | Session keep-alive |

### Commands

```bash
# Start all services
docker-compose up --build

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

---

## Project Structure

```
src/
├── v1_imap_stateless.py      # Stateless IMAP handler
├── v1_smtp_stateless.py      # Stateless SMTP handler
├── v2_imap_memory_pool.py    # In-memory IMAP pool
├── v2_smtp_memory_pool.py    # In-memory SMTP pool
├── v3_imap_redis_pool.py     # Redis-backed IMAP pool
├── v3_smtp_redis_pool.py     # Redis-backed SMTP pool
├── v3_transformer_rag.py     # Email-to-RAG transformer
├── v3_proxy_api.py           # FastAPI proxy with SDK interface
├── session_worker.py         # Background session keep-alive
└── main.py                   # Entry point

tests/
├── test_v1_*.py              # Phase 1 tests
├── test_v2_*.py              # Phase 2 tests
├── test_v3_*.py              # Phase 3 tests
├── integration/              # E2E tests
│   └── test_full_proxy.py
└── benchmark/                # Performance tests
    └── test_latency.py
```

---

## Running Tests

```bash
# All unit tests (63 tests)
pytest tests/ -v --ignore=tests/integration --ignore=tests/benchmark

# E2E integration tests (requires Gmail + Redis)
pytest tests/integration/ -v -s

# Benchmark tests
pytest tests/benchmark/ -v -s

# All tests including integration
pytest tests/ -v -s
```

### Test Summary

| Category | Tests | Requirements |
|----------|-------|--------------|
| Unit tests | 63 | None |
| Integration | 5 | Gmail + Redis |
| Benchmark | 7 | Gmail + Redis |
| **Total** | **75** | - |

---

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `TEST_GMAIL_EMAIL` | Gmail address for testing | `you@gmail.com` |
| `TEST_GMAIL_PASSWORD` | Gmail App Password | `xxxx xxxx xxxx xxxx` |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |
| `NOOP_INTERVAL` | Session worker interval (seconds) | `25` |
| `AGENTMAIL_API_KEY` | API key for proxy auth | `your-api-key` |

---

## Technical Notes

### TLS Session Caching
The OS/network layer caches TLS sessions (first connection ~300ms, subsequent ~50ms). This optimization happens *beneath* our code but **does not eliminate application-layer overhead**. Each stateless call still requires fresh TCP connection, IMAP/SMTP LOGIN, and LOGOUT. Connection pooling at the application layer is still necessary.

### .benchmarks Folder
The `.benchmarks` folder is created by `pytest-benchmark` to store historical benchmark data. It allows comparing performance across runs. Add to `.gitignore` (already done).

```bash
# Save benchmark results
pytest tests/benchmark/ --benchmark-save=baseline

# Compare with previous run
pytest tests/benchmark/ --benchmark-compare=baseline
```
