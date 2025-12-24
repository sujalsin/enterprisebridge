"""Tests for Redis-backed IMAP connection pool (Phase 3).

These tests verify:
1. Session persistence across app restarts
2. Warm connection latency
3. TTL refresh mechanism
4. Concurrent request handling

NOTE: These tests require a running Redis instance.
      Tests will be skipped if Redis is not available.
"""

import os
import asyncio
import time

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from dotenv import load_dotenv

from src.v3_imap_redis_pool import RedisIMAPPool, HybridIMAPHandler

load_dotenv()


async def check_redis_available(redis_url: str) -> bool:
    """Check if Redis is available."""
    try:
        import aioredis
        redis = aioredis.from_url(redis_url, decode_responses=True)
        await redis.ping()
        await redis.close()
        return True
    except Exception:
        return False


@pytest.fixture
def redis_url():
    """Redis URL from environment or default."""
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
async def redis_available(redis_url):
    """Check if Redis is available and skip if not."""
    available = await check_redis_available(redis_url)
    if not available:
        pytest.skip("Redis not available - skipping test")
    return True


@pytest.fixture
def gmail_creds():
    """Test Gmail credentials (mock)."""
    return {
        "host": "imap.gmail.com",
        "user": "test@gmail.com",
        "password": "test_password",
    }


@pytest.fixture
def real_gmail_creds():
    """Real Gmail credentials from .env."""
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    if not email or not password:
        pytest.skip("TEST_GMAIL_EMAIL and TEST_GMAIL_PASSWORD required")
    return {"host": "imap.gmail.com", "user": email, "password": password}


# ============================================================
# UNIT TESTS (with mocks - no Redis required)
# ============================================================

@pytest.mark.asyncio
async def test_redis_pool_survives_restart(redis_url, redis_available):
    """
    Session persists across app restarts.
    
    This is the KEY advantage of Redis over in-memory pooling.
    """
    # Create first pool instance
    pool1 = RedisIMAPPool(redis_url)
    
    session_data = {
        "host": "imap.gmail.com",
        "user": "user@acme.com",
        "auth_token": "test_token",
    }
    
    # Store session
    await pool1.store_session("user@acme.com", session_data)
    await pool1.close()
    
    # Simulate restart (new pool instance)
    pool2 = RedisIMAPPool(redis_url)
    
    # Session should survive
    recovered_session = await pool2.get_session("user@acme.com")
    
    assert recovered_session is not None, "Session should survive restart"
    assert recovered_session["host"] == "imap.gmail.com"
    assert recovered_session["user"] == "user@acme.com"
    
    # Cleanup
    await pool2.delete_session("user@acme.com")
    await pool2.close()


@pytest.mark.asyncio
async def test_session_ttl_refresh(redis_url, redis_available):
    """Background worker extends TTL."""
    pool = RedisIMAPPool(redis_url, default_ttl=60)
    
    session_data = {"host": "imap.gmail.com", "user": "ttl_test@acme.com"}
    
    # Store with 60s TTL
    await pool.store_session("ttl_test@acme.com", session_data, ttl=60)
    
    # Check initial TTL
    initial_ttl = await pool.get_ttl("ttl_test@acme.com")
    assert initial_ttl > 55, f"Initial TTL should be ~60, got {initial_ttl}"
    
    # Wait a bit
    await asyncio.sleep(2)
    
    # Refresh TTL
    await pool.refresh_ttl("ttl_test@acme.com", ttl=60)
    
    # TTL should be back to ~60
    refreshed_ttl = await pool.get_ttl("ttl_test@acme.com")
    assert refreshed_ttl > 55, f"Refreshed TTL should be ~60, got {refreshed_ttl}"
    
    print(f"\n[TTL] Initial: {initial_ttl}s → Refreshed: {refreshed_ttl}s")
    
    # Cleanup
    await pool.delete_session("ttl_test@acme.com")
    await pool.close()


@pytest.mark.asyncio
async def test_session_expiry(redis_url, redis_available):
    """Session expires after TTL."""
    pool = RedisIMAPPool(redis_url)
    
    session_data = {"host": "imap.gmail.com", "user": "expiry@acme.com"}
    
    # Store with very short TTL
    await pool.store_session("expiry@acme.com", session_data, ttl=2)
    
    # Should exist immediately
    session = await pool.get_session("expiry@acme.com")
    assert session is not None
    
    # Wait for expiry
    await asyncio.sleep(3)
    
    # Should be gone
    expired_session = await pool.get_session("expiry@acme.com")
    assert expired_session is None, "Session should expire after TTL"
    
    await pool.close()


@pytest.mark.asyncio
async def test_pool_stats(redis_url, redis_available):
    """Pool tracks hit/miss statistics."""
    pool = RedisIMAPPool(redis_url)
    
    # Miss
    await pool.get_session("nonexistent@acme.com")
    
    # Store and hit
    await pool.store_session("hit_test@acme.com", {"user": "hit_test"})
    await pool.get_session("hit_test@acme.com")
    await pool.get_session("hit_test@acme.com")
    
    stats = pool.get_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 2/3
    
    # Cleanup
    await pool.delete_session("hit_test@acme.com")
    await pool.close()


@pytest.mark.asyncio
async def test_handler_creates_and_reuses_connection(redis_url, gmail_creds, redis_available):
    """Handler reuses in-memory connections."""
    pool = RedisIMAPPool(redis_url)
    handler = HybridIMAPHandler(pool, gmail_creds)
    
    with patch("src.v3_imap_redis_pool.aioimaplib.IMAP4_SSL") as mock_imap_class:
        mock_imap = AsyncMock()
        mock_imap_class.return_value = mock_imap
        
        mock_email = b"""From: sender@example.com
Subject: Test
Date: Mon, 23 Dec 2024 10:00:00 +0000

Body."""
        mock_imap.search.return_value = ("OK", [b"1"])
        mock_imap.fetch.return_value = ("OK", [(b"1", mock_email)])
        
        # First call - creates connection
        await handler.fetch_messages(folder="INBOX", limit=1)
        
        # Second call - reuses connection
        await handler.fetch_messages(folder="INBOX", limit=1)
        
        # Should only login once
        assert mock_imap.login.call_count == 1, "Should reuse connection"
        
        # Should select twice (once per call)
        assert mock_imap.select.call_count == 2
    
    await pool.close()


# ============================================================
# INTEGRATION TESTS (with real Redis and Gmail)
# ============================================================

class TestRealRedisPool:
    """Integration tests with real Redis and Gmail."""

    @pytest.mark.asyncio
    async def test_warm_connection_latency(self, redis_url, redis_available, real_gmail_creds):
        """
        Should be <200ms with warm connection.
        
        First call: Creates connection (~800ms)
        Second call: Reuses connection (<200ms)
        """
        pool = RedisIMAPPool(redis_url)
        handler = HybridIMAPHandler(pool, real_gmail_creds)
        
        # First call (cold)
        result1 = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        cold_time = result1["timing"]["total_ms"]
        
        # Second call (warm)
        result2 = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        warm_time = result2["timing"]["total_ms"]
        
        print(f"\n[REDIS POOL] Cold: {cold_time:.0f}ms")
        print(f"[REDIS POOL] Warm: {warm_time:.0f}ms")
        print(f"[REDIS POOL] Speedup: {cold_time/warm_time:.1f}x")
        
        # Warm should be fast (connection reused)
        assert warm_time < cold_time, "Warm call should be faster"
        # Note: Gmail can have variable latency, so we use a generous threshold
        assert warm_time < 1000, f"Warm call should be <1000ms, got {warm_time:.0f}ms"
        
        # Cleanup
        await handler.close_all()
        await pool.delete_session(real_gmail_creds["user"])
        await pool.close()

    @pytest.mark.asyncio
    async def test_rapid_sequential_requests(self, redis_url, redis_available, real_gmail_creds):
        """
        10 rapid sequential requests should reuse connection efficiently.
        
        Note: IMAP connections aren't thread-safe, so we test sequential reuse.
        For true concurrency, you'd need a connection-per-request or locking.
        """
        pool = RedisIMAPPool(redis_url, max_connections=10)
        handler = HybridIMAPHandler(pool, real_gmail_creds)
        
        # Warm up
        await handler.fetch_messages(folder="INBOX", limit=1)
        
        # 10 rapid sequential requests
        times = []
        for i in range(10):
            result = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
            times.append(result["timing"]["total_ms"])
        
        avg_time = sum(times) / len(times)
        total_time = sum(times)
        
        print(f"\n[RAPID SEQUENTIAL] Times: {[f'{t:.0f}ms' for t in times[:5]]}...")
        print(f"[RAPID SEQUENTIAL] Average: {avg_time:.0f}ms")
        print(f"[RAPID SEQUENTIAL] Total (10 requests): {total_time:.0f}ms")
        print(f"[RAPID SEQUENTIAL] Reused: {pool.stats['reused']}")
        print(f"[RAPID SEQUENTIAL] Created: {pool.stats['created']}")
        
        # All requests should reuse the connection (only 1 created)
        assert pool.stats["created"] == 1, "Should only create 1 connection"
        assert pool.stats["reused"] >= 9, "Should reuse connection 9+ times"
        
        # Cleanup
        await handler.close_all()
        await pool.delete_session(real_gmail_creds["user"])
        await pool.close()

    @pytest.mark.asyncio
    async def test_redis_vs_memory_vs_stateless(self, redis_url, redis_available, real_gmail_creds):
        """Compare all three approaches."""
        from src.v1_imap_stateless import StatelessIMAPHandler
        from src.v2_imap_memory_pool import InMemoryIMAPPool, PooledIMAPHandler
        
        # v1: Stateless
        stateless = StatelessIMAPHandler(real_gmail_creds)
        
        # v2: Memory pool
        mem_pool = InMemoryIMAPPool(max_connections=5)
        mem_handler = PooledIMAPHandler(mem_pool, real_gmail_creds)
        
        # v3: Redis pool
        redis_pool = RedisIMAPPool(redis_url)
        redis_handler = HybridIMAPHandler(redis_pool, real_gmail_creds)
        
        # Warm up v2 and v3
        await mem_handler.fetch_messages(folder="INBOX", limit=1)
        await redis_handler.fetch_messages(folder="INBOX", limit=1)
        
        # Measure each
        v1_result = await stateless.fetch_messages_instrumented(folder="INBOX", limit=1)
        v2_result = await mem_handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        v3_result = await redis_handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        
        v1_time = v1_result["timing"]["total_ms"]
        v2_time = v2_result["timing"]["total_ms"]
        v3_time = v3_result["timing"]["total_ms"]
        
        print(f"\n{'='*60}")
        print("v1 vs v2 vs v3 COMPARISON")
        print(f"{'='*60}")
        print(f"  Stateless (v1):    {v1_time:.0f}ms")
        print(f"  Memory Pool (v2):  {v2_time:.0f}ms")
        print(f"  Redis Pool (v3):   {v3_time:.0f}ms")
        print(f"{'='*60}")
        print(f"  v1 → v2 improvement: {v1_time/v2_time:.1f}x")
        print(f"  v1 → v3 improvement: {v1_time/v3_time:.1f}x")
        print(f"{'='*60}")
        
        # Cleanup
        await mem_pool.close_all()
        await redis_handler.close_all()
        await redis_pool.delete_session(real_gmail_creds["user"])
        await redis_pool.close()
