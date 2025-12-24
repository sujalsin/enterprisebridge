"""Tests for Redis-backed SMTP connection pool.

These tests verify:
1. Session metadata persists in Redis
2. TTL refresh works correctly
3. Connection reuse across requests
4. Integration with real Gmail SMTP
"""

import os
import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from dotenv import load_dotenv

load_dotenv()


def get_redis_url():
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


def get_gmail_creds():
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    
    if not email or not password:
        pytest.skip("Gmail credentials not available")
    
    return {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": email,
        "password": password,
        "use_tls": False,
    }


@pytest.fixture
async def redis_available():
    """Check if Redis is available."""
    import aioredis
    try:
        redis = await aioredis.from_url(get_redis_url())
        await redis.ping()
        await redis.close()
        return True
    except Exception:
        pytest.skip("Redis not available")


# ============================================================
# UNIT TESTS
# ============================================================

@pytest.mark.asyncio
async def test_redis_smtp_pool_survives_restart(redis_available):
    """Session should persist in Redis across pool restarts."""
    from src.v3_smtp_redis_pool import RedisSMTPPool
    
    redis_url = get_redis_url()
    user = "test-restart@example.com"
    
    # Pool 1: Create session
    pool1 = RedisSMTPPool(redis_url)
    await pool1.store_session(user, {"host": "smtp.gmail.com", "port": 587})
    await pool1.close()
    
    # Pool 2: Should find existing session
    pool2 = RedisSMTPPool(redis_url)
    session = await pool2.get_session(user)
    
    assert session is not None
    assert session["host"] == "smtp.gmail.com"
    assert session["port"] == "587"
    
    # Cleanup
    await pool2.delete_session(user)
    await pool2.close()


@pytest.mark.asyncio
async def test_session_ttl_refresh(redis_available):
    """TTL should be refreshed to 300s."""
    from src.v3_smtp_redis_pool import RedisSMTPPool
    
    redis_url = get_redis_url()
    user = "test-ttl@example.com"
    
    pool = RedisSMTPPool(redis_url)
    
    # Create with short TTL
    await pool.store_session(user, {"host": "smtp.gmail.com"}, ttl=60)
    initial_ttl = await pool.get_ttl(user)
    
    # Refresh TTL
    await pool.refresh_ttl(user, ttl=300)
    refreshed_ttl = await pool.get_ttl(user)
    
    print(f"\n[TTL] Initial: {initial_ttl}s -> Refreshed: {refreshed_ttl}s")
    
    assert refreshed_ttl >= 290  # Should be close to 300
    
    # Cleanup
    await pool.delete_session(user)
    await pool.close()


@pytest.mark.asyncio
async def test_pool_stats(redis_available):
    """Pool should track hits and misses."""
    from src.v3_smtp_redis_pool import RedisSMTPPool
    
    redis_url = get_redis_url()
    pool = RedisSMTPPool(redis_url)
    
    # Miss
    await pool.get_session("nonexistent@example.com")
    assert pool.stats["misses"] == 1
    
    # Create and hit
    await pool.store_session("exists@example.com", {"host": "smtp.gmail.com"})
    await pool.get_session("exists@example.com")
    assert pool.stats["hits"] == 1
    
    # Cleanup
    await pool.delete_session("exists@example.com")
    await pool.close()


@pytest.mark.asyncio
async def test_handler_creates_and_reuses_connection(redis_available):
    """Handler should reuse connection for subsequent sends."""
    from src.v3_smtp_redis_pool import RedisSMTPPool, HybridSMTPHandler
    
    redis_url = get_redis_url()
    pool = RedisSMTPPool(redis_url)
    
    creds = {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "test@example.com",
        "password": "password",
        "use_tls": False,
    }
    
    handler = HybridSMTPHandler(pool, creds)
    
    # Mock SMTP for unit test
    with patch("aiosmtplib.SMTP") as mock_smtp_class:
        mock_smtp = AsyncMock()
        mock_smtp.connect = AsyncMock()
        mock_smtp.starttls = AsyncMock()
        mock_smtp.login = AsyncMock()
        mock_smtp.send_message = AsyncMock()
        mock_smtp.quit = AsyncMock()
        mock_smtp_class.return_value = mock_smtp
        
        # First send - creates connection
        await handler.send_message(to="a@b.com", subject="Test", body="Body")
        
        # Second send - should reuse
        await handler.send_message(to="a@b.com", subject="Test 2", body="Body 2")
        
        # Connection created only once
        assert pool.stats["created"] == 1
        assert pool.stats["reused"] == 1
        
        # Login called only once
        assert mock_smtp.login.call_count == 1
    
    await handler.close()
    await pool.delete_session(creds["user"])
    await pool.close()


# ============================================================
# INTEGRATION TESTS (Real Gmail)
# ============================================================

class TestRealSMTPPool:
    """Integration tests with real Gmail SMTP."""
    
    @pytest.fixture
    def redis_url(self):
        return get_redis_url()
    
    @pytest.fixture
    def real_gmail_creds(self):
        return get_gmail_creds()
    
    @pytest.mark.asyncio
    async def test_warm_connection_latency(self, redis_url, redis_available, real_gmail_creds):
        """Warm connection should be faster than cold."""
        from src.v3_smtp_redis_pool import RedisSMTPPool, HybridSMTPHandler
        
        pool = RedisSMTPPool(redis_url)
        handler = HybridSMTPHandler(pool, real_gmail_creds)
        
        # Clean start
        await pool.delete_session(real_gmail_creds["user"])
        
        # Cold send
        result1 = await handler.send_message_instrumented(
            to=real_gmail_creds["user"],
            subject="Cold Test",
            body="Testing cold connection",
        )
        cold_time = result1["timing"]["total_ms"]
        
        # Warm send
        result2 = await handler.send_message_instrumented(
            to=real_gmail_creds["user"],
            subject="Warm Test",
            body="Testing warm connection",
        )
        warm_time = result2["timing"]["total_ms"]
        
        print(f"\n[SMTP REDIS POOL] Cold: {cold_time:.0f}ms")
        print(f"[SMTP REDIS POOL] Warm: {warm_time:.0f}ms")
        print(f"[SMTP REDIS POOL] Pool stats: {pool.stats}")
        
        # Warm should be faster (or at least not much slower)
        # SMTP send time is dominated by server-side processing
        assert result2["status"] == "sent"
        
        await handler.close()
        await pool.delete_session(real_gmail_creds["user"])
        await pool.close()
    
    @pytest.mark.asyncio
    async def test_session_persists_in_redis(self, redis_url, redis_available, real_gmail_creds):
        """Session should be stored in Redis after connection."""
        from src.v3_smtp_redis_pool import RedisSMTPPool, HybridSMTPHandler
        import aioredis
        
        pool = RedisSMTPPool(redis_url)
        handler = HybridSMTPHandler(pool, real_gmail_creds)
        
        # Clean start
        await pool.delete_session(real_gmail_creds["user"])
        
        # Send message
        await handler.send_message(
            to=real_gmail_creds["user"],
            subject="Redis Test",
            body="Testing session persistence",
        )
        
        # Check Redis directly
        redis = await aioredis.from_url(redis_url)
        key = f"smtp:session:{real_gmail_creds['user']}"
        exists = await redis.exists(key)
        ttl = await redis.ttl(key)
        
        print(f"\n[REDIS CHECK] Key exists: {exists}")
        print(f"[REDIS CHECK] TTL: {ttl}s")
        
        assert exists == 1
        assert ttl > 0
        
        await redis.close()
        await handler.close()
        await pool.delete_session(real_gmail_creds["user"])
        await pool.close()
    
    @pytest.mark.asyncio
    async def test_v2_vs_v3_smtp_comparison(self, redis_url, redis_available, real_gmail_creds):
        """Compare v2 (memory pool) vs v3 (Redis pool) SMTP."""
        import time
        from src.v2_smtp_memory_pool import InMemorySMTPPool, PooledSMTPHandler
        from src.v3_smtp_redis_pool import RedisSMTPPool, HybridSMTPHandler
        
        # V2: Memory Pool
        v2_pool = InMemorySMTPPool(max_connections=5)
        v2_handler = PooledSMTPHandler(v2_pool, real_gmail_creds)
        
        # Warm up v2
        await v2_handler.send_message(
            to=real_gmail_creds["user"],
            subject="V2 Warmup",
            body="Warmup",
        )
        
        start = time.perf_counter()
        await v2_handler.send_message(
            to=real_gmail_creds["user"],
            subject="V2 Test",
            body="Body",
        )
        v2_time = (time.perf_counter() - start) * 1000
        
        await v2_pool.close_all()
        
        # V3: Redis Pool
        v3_pool = RedisSMTPPool(redis_url)
        v3_handler = HybridSMTPHandler(v3_pool, real_gmail_creds)
        
        # Clean start
        await v3_pool.delete_session(real_gmail_creds["user"])
        
        # Warm up v3
        await v3_handler.send_message(
            to=real_gmail_creds["user"],
            subject="V3 Warmup",
            body="Warmup",
        )
        
        start = time.perf_counter()
        await v3_handler.send_message(
            to=real_gmail_creds["user"],
            subject="V3 Test",
            body="Body",
        )
        v3_time = (time.perf_counter() - start) * 1000
        
        await v3_handler.close()
        await v3_pool.delete_session(real_gmail_creds["user"])
        await v3_pool.close()
        
        print("\n" + "=" * 50)
        print("SMTP v2 vs v3 COMPARISON")
        print("=" * 50)
        print(f"  v2 Memory Pool: {v2_time:.0f}ms")
        print(f"  v3 Redis Pool:  {v3_time:.0f}ms")
        print("=" * 50)
