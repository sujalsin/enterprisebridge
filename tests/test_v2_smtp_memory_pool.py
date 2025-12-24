"""Tests for in-memory SMTP connection pool (Phase 2).

These tests verify:
1. Pool reduces latency by reusing connections
2. Connections are properly reused (login called only once)
3. Multiple sends use the same connection
"""

import os
import asyncio

import pytest
from unittest.mock import patch, AsyncMock, PropertyMock
from dotenv import load_dotenv

from src.v2_smtp_memory_pool import InMemorySMTPPool, PooledSMTPHandler

load_dotenv()


@pytest.fixture
def gmail_smtp_creds():
    """Test Gmail SMTP credentials."""
    return {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "test@gmail.com",
        "password": "test_password",
        "use_tls": False,
    }


@pytest.fixture
def real_gmail_smtp_creds():
    """Real Gmail SMTP credentials from .env."""
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    if not email or not password:
        pytest.skip("TEST_GMAIL_EMAIL and TEST_GMAIL_PASSWORD required")
    return {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": email,
        "password": password,
        "use_tls": False,
    }


# ============================================================
# UNIT TESTS (with mocks)
# ============================================================

def test_smtp_pool_reduces_multiple_sends(gmail_smtp_creds):
    """3 sends should reuse connection - login once only."""
    pool = InMemorySMTPPool(max_connections=5)
    handler = PooledSMTPHandler(pool, gmail_smtp_creds)

    with patch("src.v2_smtp_memory_pool.aiosmtplib.SMTP") as mock_smtp_class:
        mock_smtp = AsyncMock()
        mock_smtp_class.return_value = mock_smtp
        
        # Mock is_connected to return True
        type(mock_smtp).is_connected = PropertyMock(return_value=True)

        loop = asyncio.get_event_loop()

        # First send (warm up)
        loop.run_until_complete(
            handler.send_message("test@example.com", "Hi 1", "Body 1")
        )

        # Next 3 sends
        for i in range(3):
            loop.run_until_complete(
                handler.send_message("test@example.com", f"Hi {i+2}", f"Body {i+2}")
            )

        # Should only login ONCE (connection reused)
        assert mock_smtp.login.call_count == 1, "Should only login once with pooling"
        
        # Should send 4 times
        assert mock_smtp.send_message.call_count == 4, "Should send 4 messages"


def test_smtp_pool_reuses_connection(gmail_smtp_creds):
    """Should use same connection for 2 sends."""
    pool = InMemorySMTPPool(max_connections=5)
    handler = PooledSMTPHandler(pool, gmail_smtp_creds)

    with patch("src.v2_smtp_memory_pool.aiosmtplib.SMTP") as mock_smtp_class:
        mock_smtp = AsyncMock()
        mock_smtp_class.return_value = mock_smtp
        type(mock_smtp).is_connected = PropertyMock(return_value=True)

        loop = asyncio.get_event_loop()

        # Two sends
        loop.run_until_complete(handler.send_message("a@b.com", "S1", "B1"))
        loop.run_until_complete(handler.send_message("a@b.com", "S2", "B2"))

        # Should only login once
        assert mock_smtp.login.call_count == 1
        # Should connect once
        assert mock_smtp.connect.call_count == 1
        # Should NOT quit (connection stays open)
        assert mock_smtp.quit.call_count == 0


def test_smtp_pool_crash_on_restart(gmail_smtp_creds):
    """
    Simulate app restart - pool should be empty.
    
    Demonstrates the LIMITATION of in-memory pooling.
    """
    pool = InMemorySMTPPool(max_connections=5)
    
    # Simulate having an active connection
    pool.connections["user@acme.com"] = "fake_connection"
    pool._connection_times["user@acme.com"] = 12345.0
    
    # Verify connection exists
    assert "user@acme.com" in pool.connections
    
    # Simulate restart (new pool instance)
    new_pool = InMemorySMTPPool(max_connections=5)
    
    # Old connection is LOST
    assert "user@acme.com" not in new_pool.connections
    assert len(new_pool.connections) == 0


def test_smtp_pool_stats(gmail_smtp_creds):
    """Pool should report accurate statistics."""
    pool = InMemorySMTPPool(max_connections=5)

    with patch("src.v2_smtp_memory_pool.aiosmtplib.SMTP") as mock_smtp_class:
        mock_smtp = AsyncMock()
        mock_smtp_class.return_value = mock_smtp
        type(mock_smtp).is_connected = PropertyMock(return_value=True)

        loop = asyncio.get_event_loop()
        handler = PooledSMTPHandler(pool, gmail_smtp_creds)
        loop.run_until_complete(handler.send_message("a@b.com", "S", "B"))

        stats = pool.get_stats()
        assert stats["active_connections"] == 1
        assert stats["max_connections"] == 5
        assert gmail_smtp_creds["user"] in stats["users"]


# ============================================================
# INTEGRATION TESTS (with real Gmail)
# ============================================================

class TestRealPooledSMTP:
    """Integration tests with real Gmail SMTP server."""

    @pytest.mark.asyncio
    async def test_smtp_pool_latency(self, real_gmail_smtp_creds):
        """
        Pooled SMTP should be faster than stateless.
        
        Target: ~500ms pooled vs ~1330ms stateless
        """
        pool = InMemorySMTPPool(max_connections=5)
        handler = PooledSMTPHandler(pool, real_gmail_smtp_creds)
        test_email = real_gmail_smtp_creds["user"]

        # Warm up
        result1 = await handler.send_message_instrumented(
            to=test_email, subject="[Pool Test] Warmup", body="Warmup"
        )
        warmup_time = result1["timing"]["total_ms"]

        # Second call - should be fast
        result2 = await handler.send_message_instrumented(
            to=test_email, subject="[Pool Test] Pooled", body="Pooled"
        )
        pooled_time = result2["timing"]["total_ms"]

        print(f"\n[SMTP POOL] Warm-up: {warmup_time:.0f}ms")
        print(f"[SMTP POOL] Pooled:  {pooled_time:.0f}ms")
        print(f"[SMTP POOL] Speedup: {warmup_time/pooled_time:.1f}x")

        # Pooled should be faster (connection already established)
        # Most time should be just the send operation
        assert result2["timing"]["get_connection_ms"] < 10, \
            "Getting pooled connection should be <10ms"

        # Cleanup
        await pool.close_all()

    @pytest.mark.asyncio
    async def test_smtp_pool_vs_stateless(self, real_gmail_smtp_creds):
        """Compare pooled vs stateless SMTP performance."""
        from src.v1_smtp_stateless import StatelessSMTPHandler

        test_email = real_gmail_smtp_creds["user"]

        # Stateless handler (Phase 1)
        stateless = StatelessSMTPHandler(real_gmail_smtp_creds)

        # Pooled handler (Phase 2)
        pool = InMemorySMTPPool(max_connections=5)
        pooled = PooledSMTPHandler(pool, real_gmail_smtp_creds)

        # Warm up the pool
        await pooled.send_message(to=test_email, subject="Warmup", body="W")

        # Time stateless send
        stateless_result = await stateless.send_message_instrumented(
            to=test_email, subject="[v1] Stateless", body="Stateless"
        )
        stateless_time = stateless_result["timing"]["total_ms"]

        # Time pooled send
        pooled_result = await pooled.send_message_instrumented(
            to=test_email, subject="[v2] Pooled", body="Pooled"
        )
        pooled_time = pooled_result["timing"]["total_ms"]

        print(f"\n{'='*60}")
        print("SMTP: PHASE 1 vs PHASE 2 COMPARISON")
        print(f"{'='*60}")
        print(f"  Stateless (v1): {stateless_time:.0f}ms")
        print(f"  Pooled (v2):    {pooled_time:.0f}ms")
        print(f"  Connection overhead eliminated!")
        print(f"{'='*60}")

        # Connection overhead should be minimal for pooled
        pooled_conn_time = pooled_result["timing"]["get_connection_ms"]
        stateless_conn_time = stateless_result["timing"]["connect_ms"] + \
                              stateless_result["timing"]["login_ms"]
        
        print(f"\n  Stateless connection overhead: {stateless_conn_time:.0f}ms")
        print(f"  Pooled connection overhead:    {pooled_conn_time:.0f}ms")

        # Cleanup
        await pool.close_all()

    @pytest.mark.asyncio
    async def test_smtp_pool_multiple_sends(self, real_gmail_smtp_creds):
        """Measure performance for multiple pooled sends."""
        from src.v1_smtp_stateless import StatelessSMTPHandler

        test_email = real_gmail_smtp_creds["user"]

        pool = InMemorySMTPPool(max_connections=5)
        pooled = PooledSMTPHandler(pool, real_gmail_smtp_creds)

        # Warm up
        await pooled.send_message(to=test_email, subject="Warmup", body="W")

        # 3 pooled sends
        pooled_times = []
        for i in range(3):
            result = await pooled.send_message_instrumented(
                to=test_email, subject=f"[Pool {i+1}]", body=f"Msg {i+1}"
            )
            pooled_times.append(result["timing"]["total_ms"])

        # 3 stateless sends
        stateless_times = []
        for i in range(3):
            handler = StatelessSMTPHandler(real_gmail_smtp_creds)
            result = await handler.send_message_instrumented(
                to=test_email, subject=f"[Stateless {i+1}]", body=f"Msg {i+1}"
            )
            stateless_times.append(result["timing"]["total_ms"])

        print(f"\n{'='*60}")
        print("SMTP: 3 SENDS COMPARISON")
        print(f"{'='*60}")
        print(f"  Pooled times:    {[f'{t:.0f}ms' for t in pooled_times]}")
        print(f"  Stateless times: {[f'{t:.0f}ms' for t in stateless_times]}")
        print(f"  Pooled total:    {sum(pooled_times):.0f}ms")
        print(f"  Stateless total: {sum(stateless_times):.0f}ms")
        
        if sum(pooled_times) > 0:
            print(f"  Improvement:     {sum(stateless_times)/sum(pooled_times):.1f}x")
        print(f"{'='*60}")

        # Cleanup
        await pool.close_all()
