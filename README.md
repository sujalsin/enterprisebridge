# AgentMail Proxy

A high-performance email proxy for AI agents with connection pooling, Redis session persistence, and LLM-ready email transformation.

## Quick Start

```bash
# Install
pip install -e .

# Configure
cp .env.example .env
# Edit .env with your Gmail credentials

# Start Redis (for v3 features)
docker run --name agentmail-redis -p 6379:6379 -d redis

# Run all tests
pytest tests/ -v
```

---

## Architecture Overview

| Version | Type | Latency | Persistence | Best For |
|---------|------|---------|-------------|----------|
| **v1** | Stateless | ~1200ms | None | Simple scripts |
| **v2** | Memory Pool | ~300ms | Process lifetime | Single-process apps |
| **v3** | Redis Pool | ~400ms | Survives restarts | Production deployments |

---

## Phase 1: Stateless Handlers (Baseline)

Naive handlers that create fresh connections for every operation.

### Benchmark Results (Real Gmail)

| Test | Time |
|------|------|
| IMAP Fetch 3 messages | 1.81s |
| IMAP Multiple calls (3x) | 3.45s total |
| SMTP Send message | 1.33s |
| SMTP Multiple sends (3x) | 4.49s total |
| Combined workflow | **3.25s** |

### Application-Layer Breakdown

| Phase | IMAP | SMTP |
|-------|------|------|
| Connect (TCP+TLS) | 17ms | 20ms |
| Login | 257ms | 146ms |
| Operation | 481ms | 1070ms |
| Disconnect | 96ms | 1ms |
| **Overhead %** | **43%** | **13%** |

> **Key Finding**: IMAP 3x calls waste **72%** of time on connection overhead!

---

## Phase 2: In-Memory Connection Pool

Maintains persistent connections within a process.

### Results vs Stateless

| Metric | v1 Stateless | v2 Pooled | Improvement |
|--------|--------------|-----------|-------------|
| IMAP call | 1225ms | 310ms | **4.0x faster** |
| 3 IMAP calls | 2929ms | 944ms | **3.1x faster** |
| SMTP send | 1947ms | 1801ms | 1.1x |

### Limitation
- Connections lost on application restart
- Not shared across processes

---

## Phase 3: Redis-Backed Pool

Session metadata persists in Redis, surviving restarts.

### Features
- Session persistence across restarts
- TTL-based session expiry with refresh
- Connection reuse within process
- Hit/miss statistics tracking

### Results

| Metric | v1 | v2 Memory | v3 Redis |
|--------|-----|-----------|----------|
| Latency | 1915ms | 541ms | 774ms |
| Improvement | - | 3.5x | 2.5x |
| Persistence | No | No | Yes |

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
| Signature removal | PASS - Removes `div.signature`, `email-signature` |
| Quote collapsing | PASS - `>>>` becomes `[Quoted text collapsed]` |
| Tracking pixel removal | PASS - Strips 1x1 images |
| PDF text extraction | PASS - Extracts via mock/OCR |
| Size reduction | PASS - 50%+ reduction achieved |
| Content truncation | PASS - Truncates to 5000 chars |

### Example

```python
from src.v3_transformer_rag import transform_to_rag

result = transform_to_rag(mime_data)

# Returns:
{
    "body": "Clean markdown text...",
    "subject": "Meeting Tomorrow",
    "from": "sender@example.com",
    "attachments": [
        {"filename": "invoice.pdf", "extracted_text": "..."}
    ],
    "thread_id": "abc123def456"
}
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
├── v3_transformer_rag.py     # Email-to-RAG transformer
└── main.py                   # Entry point

tests/
├── test_v1_imap_stateless.py
├── test_v1_smtp_stateless.py
├── test_v2_imap_memory_pool.py
├── test_v2_smtp_memory_pool.py
├── test_v3_imap_redis_pool.py
├── test_v3_transformer_rag.py
├── test_integration_real_servers.py
└── test_latency_breakdown.py
```

---

## Running Tests

```bash
# All unit tests
pytest tests/ -v

# Integration tests (requires Gmail credentials)
pytest tests/test_integration_real_servers.py -v -s

# Latency benchmarks
pytest tests/test_latency_breakdown.py -v -s

# Redis pool tests (requires Redis)
pytest tests/test_v3_imap_redis_pool.py -v -s

# RAG transformer tests
pytest tests/test_v3_transformer_rag.py -v
```

---

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `TEST_GMAIL_EMAIL` | Gmail address for testing | `you@gmail.com` |
| `TEST_GMAIL_PASSWORD` | Gmail App Password | `xxxx xxxx xxxx xxxx` |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |

---

## Technical Notes

> **TLS Session Caching**: The OS/network layer caches TLS sessions (first connection ~300ms, subsequent ~50ms). This optimization happens *beneath* our code but **does not eliminate application-layer overhead** - each stateless call still requires a fresh TCP connection, IMAP/SMTP LOGIN, and LOGOUT. Connection pooling at the application layer is still necessary.
