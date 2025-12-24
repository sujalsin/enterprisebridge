"""Tests for in-memory IMAP connection pool (Phase 2).

These tests verify:
1. Pool reduces latency by reusing connections
2. Connections are properly reused (login called only once)
3. Connections are lost on app restart (demonstrating the limitation)
"""

import os
import asyncio

import pytest
from unittest.mock import patch, AsyncMock
from dotenv import load_dotenv

from src.v2_imap_memory_pool import InMemoryIMAPPool, PooledIMAPHandler

load_dotenv()


@pytest.fixture
def gmail_creds():
    """Test Gmail credentials."""
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
# UNIT TESTS (with mocks)
# ============================================================

def test_pool_reuses_connection(gmail_creds):
    """Should use same connection for 2 calls - login once only."""
    pool = InMemoryIMAPPool(max_connections=5)
    handler = PooledIMAPHandler(pool, gmail_creds)

    with patch("src.v2_imap_memory_pool.aioimaplib.IMAP4_SSL") as mock_imap_class:
        mock_imap = AsyncMock()
        mock_imap_class.return_value = mock_imap

        # Create valid RFC822 email mock
        mock_email = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Email
Date: Mon, 23 Dec 2024 10:00:00 +0000

Test body."""

        mock_imap.search.return_value = ("OK", [b"1 2 3"])
        mock_imap.fetch.return_value = ("OK", [(b"1", mock_email)])

        loop = asyncio.get_event_loop()

        # First call - should create connection and login
        loop.run_until_complete(handler.fetch_messages(folder="INBOX", limit=3))
        
        # Second call - should reuse connection (no new login)
        loop.run_until_complete(handler.fetch_messages(folder="INBOX", limit=3))

        # Should only login ONCE (connection reused)
        assert mock_imap.login.call_count == 1, "Should only login once with pooling"
        
        # But select should be called twice (once per fetch)
        assert mock_imap.select.call_count == 2, "Should select folder each time"


def test_pool_crash_on_restart(gmail_creds):
    """
    Simulate app restart - pool should be empty.
    
    This demonstrates the LIMITATION of in-memory pooling:
    connections are lost when the application restarts.
    """
    pool = InMemoryIMAPPool(max_connections=5)
    
    # Simulate having an active connection
    pool.connections["user@acme.com"] = "fake_connection"
    pool._connection_times["user@acme.com"] = 12345.0
    
    # Verify connection exists
    assert "user@acme.com" in pool.connections
    
    # Simulate restart (new pool instance)
    new_pool = InMemoryIMAPPool(max_connections=5)
    
    # Old connection is LOST - this is the failure we want to demonstrate
    assert "user@acme.com" not in new_pool.connections, \
        "In-memory pool loses connections on restart"
    assert len(new_pool.connections) == 0, \
        "New pool should be empty"


def test_pool_max_connections(gmail_creds):
    """Pool should evict oldest connection when max reached."""
    pool = InMemoryIMAPPool(max_connections=2)

    with patch("src.v2_imap_memory_pool.aioimaplib.IMAP4_SSL") as mock_imap_class:
        mock_imap = AsyncMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.search.return_value = ("OK", [b""])

        loop = asyncio.get_event_loop()

        # Create 3 handlers with different users
        creds1 = {**gmail_creds, "user": "user1@test.com"}
        creds2 = {**gmail_creds, "user": "user2@test.com"}
        creds3 = {**gmail_creds, "user": "user3@test.com"}

        handler1 = PooledIMAPHandler(pool, creds1)
        handler2 = PooledIMAPHandler(pool, creds2)
        handler3 = PooledIMAPHandler(pool, creds3)

        # Connect all three
        loop.run_until_complete(handler1.fetch_messages(folder="INBOX", limit=1))
        loop.run_until_complete(handler2.fetch_messages(folder="INBOX", limit=1))
        loop.run_until_complete(handler3.fetch_messages(folder="INBOX", limit=1))

        # Pool should only have 2 connections (max)
        assert len(pool.connections) <= 2, "Pool should respect max_connections"


def test_pool_stats(gmail_creds):
    """Pool should report accurate statistics."""
    pool = InMemoryIMAPPool(max_connections=5)

    with patch("src.v2_imap_memory_pool.aioimaplib.IMAP4_SSL") as mock_imap_class:
        mock_imap = AsyncMock()
        mock_imap_class.return_value = mock_imap
        mock_imap.search.return_value = ("OK", [b""])

        loop = asyncio.get_event_loop()
        handler = PooledIMAPHandler(pool, gmail_creds)
        loop.run_until_complete(handler.fetch_messages(folder="INBOX", limit=1))

        stats = pool.get_stats()
        assert stats["active_connections"] == 1
        assert stats["max_connections"] == 5
        assert gmail_creds["user"] in stats["users"]


# ============================================================
# INTEGRATION TESTS (with real Gmail)
# ============================================================

class TestRealPooledConnection:
    """Integration tests with real Gmail server."""

    @pytest.mark.asyncio
    async def test_memory_pool_reduces_latency(self, real_gmail_creds):
        """
        Should be ~300ms after pool warm-up.
        
        First call: Creates connection + login (~800ms)
        Second call: Reuses connection (~300ms)
        """
        pool = InMemoryIMAPPool(max_connections=5)
        handler = PooledIMAPHandler(pool, real_gmail_creds)

        # Warm up pool (first call - slow)
        result1 = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        warmup_time = result1["timing"]["total_ms"]

        # Second call - should be fast (pooled)
        result2 = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        pooled_time = result2["timing"]["total_ms"]

        print(f"\n[POOL] Warm-up call: {warmup_time:.0f}ms")
        print(f"[POOL] Pooled call:  {pooled_time:.0f}ms")
        print(f"[POOL] Speedup:      {warmup_time/pooled_time:.1f}x")

        # Pooled call should be significantly faster
        assert pooled_time < warmup_time, "Pooled call should be faster"
        assert pooled_time < 500, f"Pooled call should be <500ms, got {pooled_time:.0f}ms"

        # Cleanup
        await pool.close_all()

    @pytest.mark.asyncio
    async def test_pool_vs_stateless_comparison(self, real_gmail_creds):
        """
        Compare pooled vs stateless performance.
        
        This demonstrates the improvement from Phase 1 to Phase 2.
        """
        from src.v1_imap_stateless import StatelessIMAPHandler

        # Stateless handler (Phase 1)
        stateless = StatelessIMAPHandler(real_gmail_creds)
        
        # Pooled handler (Phase 2)
        pool = InMemoryIMAPPool(max_connections=5)
        pooled = PooledIMAPHandler(pool, real_gmail_creds)

        # Warm up the pool
        await pooled.fetch_messages(folder="INBOX", limit=1)

        # Time stateless call
        stateless_result = await stateless.fetch_messages_instrumented(folder="INBOX", limit=1)
        stateless_time = stateless_result["timing"]["total_ms"]

        # Time pooled call
        pooled_result = await pooled.fetch_messages_instrumented(folder="INBOX", limit=1)
        pooled_time = pooled_result["timing"]["total_ms"]

        print(f"\n{'='*60}")
        print("PHASE 1 vs PHASE 2 COMPARISON")
        print(f"{'='*60}")
        print(f"  Stateless (v1): {stateless_time:.0f}ms")
        print(f"  Pooled (v2):    {pooled_time:.0f}ms")
        print(f"  Improvement:    {stateless_time/pooled_time:.1f}x faster")
        print(f"{'='*60}")

        # Pooled should be significantly faster
        assert pooled_time < stateless_time, "Pooled should be faster than stateless"

        # Cleanup
        await pool.close_all()

    @pytest.mark.asyncio
    async def test_pool_multiple_calls_overhead(self, real_gmail_creds):
        """Measure overhead for 3 pooled calls vs stateless."""
        from src.v1_imap_stateless import StatelessIMAPHandler

        pool = InMemoryIMAPPool(max_connections=5)
        pooled = PooledIMAPHandler(pool, real_gmail_creds)

        # Warm up
        await pooled.fetch_messages(folder="INBOX", limit=1)

        # 3 pooled calls
        pooled_times = []
        for i in range(3):
            result = await pooled.fetch_messages_instrumented(folder="INBOX", limit=1)
            pooled_times.append(result["timing"]["total_ms"])

        # 3 stateless calls
        stateless_times = []
        for i in range(3):
            handler = StatelessIMAPHandler(real_gmail_creds)
            result = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
            stateless_times.append(result["timing"]["total_ms"])

        print(f"\n{'='*60}")
        print("3 CALLS COMPARISON")
        print(f"{'='*60}")
        print(f"  Pooled times:    {[f'{t:.0f}ms' for t in pooled_times]}")
        print(f"  Stateless times: {[f'{t:.0f}ms' for t in stateless_times]}")
        print(f"  Pooled total:    {sum(pooled_times):.0f}ms")
        print(f"  Stateless total: {sum(stateless_times):.0f}ms")
        print(f"  Improvement:     {sum(stateless_times)/sum(pooled_times):.1f}x")
        print(f"{'='*60}")

        # Cleanup
        await pool.close_all()
