"""Benchmark tests for latency and performance.

These tests measure:
1. Cold start latency (first request after flush)
2. Warm request latency (session reused)
3. Session reuse rate
4. Memory leak detection

Requirements:
- Redis running: docker run --name agentmail-redis -p 6379:6379 -d redis
- Gmail credentials in .env

Run with:
    pytest tests/benchmark/test_latency.py -v -s --benchmark-only
"""

import os
import gc
import asyncio
import pytest
from dotenv import load_dotenv

load_dotenv()

# Mark all tests in this file as benchmark tests
pytestmark = pytest.mark.benchmark


def get_gmail_creds():
    """Get Gmail credentials from environment."""
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    
    if not email or not password:
        pytest.skip("Gmail credentials not available")
    
    return {
        "host": "imap.gmail.com",
        "user": email,
        "password": password,
    }


def get_memory_rss():
    """Get current memory usage in MB."""
    import resource
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KB to MB


class TestLatencyBenchmarks:
    """Latency benchmark tests."""
    
    @pytest.fixture
    async def redis_client(self):
        """Create Redis client."""
        import aioredis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        
        try:
            redis = await aioredis.from_url(redis_url)
            yield redis
            await redis.close()
        except Exception:
            pytest.skip("Redis not available")
    
    @pytest.fixture
    async def imap_pool(self, redis_client):
        """Create IMAP pool."""
        from src.v3_imap_redis_pool import RedisIMAPPool
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        pool = RedisIMAPPool(redis_url)
        yield pool
        await pool.close()
    
    @pytest.fixture
    async def handler(self, imap_pool):
        """Create hybrid handler."""
        from src.v3_imap_redis_pool import HybridIMAPHandler
        creds = get_gmail_creds()
        handler = HybridIMAPHandler(imap_pool, creds)
        yield handler
        await handler.close_all()
    
    @pytest.mark.asyncio
    async def test_cold_start_latency(self, redis_client, handler, imap_pool):
        """First request after Redis flush - should be slow (~1-2s)."""
        creds = get_gmail_creds()
        
        # Flush Redis to simulate cold start
        await redis_client.flushdb()
        
        # Close any existing connections
        await handler.close_all()
        
        # Create fresh handler
        from src.v3_imap_redis_pool import HybridIMAPHandler
        fresh_handler = HybridIMAPHandler(imap_pool, creds)
        
        # Measure cold start
        import time
        start = time.perf_counter()
        result = await fresh_handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        cold_time = (time.perf_counter() - start) * 1000
        
        print(f"\n[BENCHMARK] Cold start: {cold_time:.0f}ms")
        print(f"[BENCHMARK] Breakdown: {result['timing']}")
        
        # Cold start should be > 500ms (includes connect + login)
        assert cold_time > 500, f"Cold start too fast: {cold_time:.0f}ms"
        
        await fresh_handler.close_all()
        await imap_pool.delete_session(creds["user"])
    
    @pytest.mark.asyncio
    async def test_warm_request_latency(self, handler, imap_pool):
        """Second request (session in Redis) - should be fast (<1s)."""
        creds = get_gmail_creds()
        
        # Warm up - first request creates session
        await handler.fetch_messages(folder="INBOX", limit=1)
        
        # Measure warm request
        import time
        start = time.perf_counter()
        result = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        warm_time = (time.perf_counter() - start) * 1000
        
        print(f"\n[BENCHMARK] Warm request: {warm_time:.0f}ms")
        print(f"[BENCHMARK] Breakdown: {result['timing']}")
        
        # Warm request should be significantly faster (connection reused)
        # Note: Gmail IMAP operations still take time
        assert warm_time < 1500, f"Warm request too slow: {warm_time:.0f}ms"
        
        # Stats should show reuse
        print(f"[BENCHMARK] Pool stats: {imap_pool.stats}")
        
        await imap_pool.delete_session(creds["user"])
    
    @pytest.mark.asyncio
    async def test_session_reuse_rate(self, handler, imap_pool):
        """Most requests should reuse sessions (>90%)."""
        creds = get_gmail_creds()
        
        # Reset stats
        imap_pool.stats = {"created": 0, "reused": 0, "hits": 0, "misses": 0}
        
        # Run 20 requests (reduced to avoid rate limits)
        num_requests = 20
        print(f"\n[BENCHMARK] Running {num_requests} requests...")
        
        for i in range(num_requests):
            await handler.fetch_messages(folder="INBOX", limit=1)
        
        stats = imap_pool.stats
        total = stats["created"] + stats["reused"]
        reuse_rate = stats["reused"] / total if total > 0 else 0
        
        print(f"[BENCHMARK] Stats: {stats}")
        print(f"[BENCHMARK] Reuse rate: {reuse_rate:.1%}")
        
        # At least 90% of requests should reuse sessions
        assert reuse_rate >= 0.90, f"Reuse rate too low: {reuse_rate:.1%}"
        
        await imap_pool.delete_session(creds["user"])
    
    @pytest.mark.asyncio
    async def test_memory_stability(self, handler, imap_pool):
        """Memory should not grow significantly over many requests."""
        creds = get_gmail_creds()
        
        # Force garbage collection
        gc.collect()
        initial_memory = get_memory_rss()
        
        # Run requests (reduced count)
        num_requests = 50
        print(f"\n[BENCHMARK] Running {num_requests} requests for memory test...")
        print(f"[BENCHMARK] Initial memory: {initial_memory:.1f} MB")
        
        for i in range(num_requests):
            await handler.fetch_messages(folder="INBOX", limit=1)
            
            # Log progress every 10 requests
            if (i + 1) % 10 == 0:
                gc.collect()
                current = get_memory_rss()
                print(f"[BENCHMARK] After {i+1} requests: {current:.1f} MB")
        
        # Final garbage collection
        gc.collect()
        final_memory = get_memory_rss()
        
        growth = (final_memory - initial_memory) / initial_memory if initial_memory > 0 else 0
        
        print(f"[BENCHMARK] Final memory: {final_memory:.1f} MB")
        print(f"[BENCHMARK] Growth: {growth:.1%}")
        
        # Memory should not grow more than 50% (generous threshold for test environment)
        assert growth < 0.50, f"Memory grew too much: {growth:.1%}"
        
        await imap_pool.delete_session(creds["user"])


class TestSyntheticBenchmarks:
    """Synthetic benchmarks using pytest-benchmark."""
    
    def test_message_parsing_speed(self, benchmark):
        """Benchmark message parsing without network."""
        from src.v3_transformer_rag import transform_to_rag
        
        # Create sample MIME data
        mime_data = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Message
Content-Type: text/html

<html>
<body>
<div>This is a test message with some content.</div>
<div class="signature">John Doe | CEO</div>
</body>
</html>
"""
        
        def parse_message():
            return transform_to_rag(mime_data)
        
        result = benchmark(parse_message)
        
        # Should be fast (< 10ms)
        assert benchmark.stats.stats.mean < 0.01, "Parsing too slow"
    
    def test_thread_id_generation_speed(self, benchmark):
        """Benchmark thread ID generation."""
        from src.v3_transformer_rag import generate_thread_id
        
        def generate():
            return generate_thread_id("<123@example.com>", "<456@example.com>")
        
        result = benchmark(generate)
        
        # Should be very fast (< 1ms)
        assert benchmark.stats.stats.mean < 0.001, "Thread ID generation too slow"


class TestComparisonBenchmarks:
    """Compare v1 vs v2 vs v3 performance."""
    
    @pytest.mark.asyncio
    async def test_v1_vs_v2_vs_v3_comparison(self):
        """Compare all three versions."""
        import time
        
        creds = get_gmail_creds()
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        
        # V1: Stateless
        from src.v1_imap_stateless import StatelessIMAPHandler
        v1_handler = StatelessIMAPHandler(creds)
        
        start = time.perf_counter()
        await v1_handler.fetch_messages(folder="INBOX", limit=1)
        v1_time = (time.perf_counter() - start) * 1000
        
        # V2: Memory Pool
        from src.v2_imap_memory_pool import InMemoryIMAPPool, PooledIMAPHandler
        v2_pool = InMemoryIMAPPool(max_connections=5)
        v2_handler = PooledIMAPHandler(v2_pool, creds)
        
        # Warm up
        await v2_handler.fetch_messages(folder="INBOX", limit=1)
        
        start = time.perf_counter()
        await v2_handler.fetch_messages(folder="INBOX", limit=1)
        v2_time = (time.perf_counter() - start) * 1000
        
        await v2_pool.close_all()
        
        # V3: Redis Pool
        from src.v3_imap_redis_pool import RedisIMAPPool, HybridIMAPHandler
        v3_pool = RedisIMAPPool(redis_url)
        v3_handler = HybridIMAPHandler(v3_pool, creds)
        
        # Warm up
        await v3_handler.fetch_messages(folder="INBOX", limit=1)
        
        start = time.perf_counter()
        await v3_handler.fetch_messages(folder="INBOX", limit=1)
        v3_time = (time.perf_counter() - start) * 1000
        
        await v3_handler.close_all()
        await v3_pool.delete_session(creds["user"])
        await v3_pool.close()
        
        # Print comparison
        print("\n" + "=" * 60)
        print("LATENCY COMPARISON (warm requests)")
        print("=" * 60)
        print(f"  v1 Stateless:   {v1_time:,.0f}ms")
        print(f"  v2 Memory Pool: {v2_time:,.0f}ms ({v1_time/v2_time:.1f}x faster)")
        print(f"  v3 Redis Pool:  {v3_time:,.0f}ms ({v1_time/v3_time:.1f}x faster)")
        print("=" * 60)
        
        # v2 and v3 should be significantly faster than v1
        assert v2_time < v1_time, "v2 should be faster than v1"
