"""Benchmark tests for complex workflows and edge cases.

Includes:
1. Full workflow latency (IMAP fetch + SMTP reply)
2. Rate limiting handling (Simulated)
"""

import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch
from dotenv import load_dotenv
import os

load_dotenv()

def get_gmail_creds():
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    if not email or not password:
        pytest.skip("Gmail credentials not available")
    return {
        "host": "imap.gmail.com",
        "user": email,
        "password": password,
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
    }

@pytest.fixture
async def redis_available():
    import aioredis
    try:
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        redis = await aioredis.from_url(redis_url)
        await redis.ping()
        await redis.close()
        return True
    except Exception:
        pytest.skip("Redis not available")

@pytest.mark.asyncio
async def test_full_workflow_latency(redis_available):
    """
    Test combined latency of fetching recent emails and processing a reply.
    Target: < 2.0s for the whole flow in v3.
    """
    from src.v3_imap_redis_pool import RedisIMAPPool, HybridIMAPHandler
    from src.v3_smtp_redis_pool import RedisSMTPPool, HybridSMTPHandler

    creds = get_gmail_creds()
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    user = creds["user"]

    # 1. Setup Pools
    imap_pool = RedisIMAPPool(redis_url)
    smtp_pool = RedisSMTPPool(redis_url)
    
    imap_handler = HybridIMAPHandler(imap_pool, creds)
    smtp_handler = HybridSMTPHandler(smtp_pool, {
        "host": creds["smtp_host"],
        "port": creds["smtp_port"],
        "user": user,
        "password": creds["password"],
        "use_tls": False,
        "start_tls": True
    })

    # Warm up connections (v3 benefit is reuse, so we assume warm state or measure cold/warm average)
    try:
        # Establish connections first (Simulate active agent)
        await imap_handler.fetch_messages("INBOX", 1)
        await smtp_handler._get_connection() 
        
        print("\n[WORKFLOW] Starting Full Workflow (Warm)...")
        start_time = time.perf_counter()

        # Step A: Fetch 3 recent messages (e.g., to generate context)
        # Using a search to find actual messages
        messages = await imap_handler.fetch_messages("INBOX", 3)
        fetch_time = time.perf_counter()
        
        # Step B: 'Process' and Send Reply (Send 1 email)
        # We'll send to self to be safe
        await smtp_handler.send_message(
            to=user,
            subject="Workflow Benchmark Reply",
            body="This is a reply generated during the full workflow benchmark."
        )
        end_time = time.perf_counter()

        total_duration = end_time - start_time
        fetch_duration = fetch_time - start_time
        send_duration = end_time - fetch_time

        print(f"[WORKFLOW] Fetch 3 msgs: {fetch_duration*1000:.1f}ms")
        print(f"[WORKFLOW] Send reply:  {send_duration*1000:.1f}ms")
        print(f"[WORKFLOW] Total:        {total_duration*1000:.1f}ms")

        # Assertion: Should be well under 2 seconds for v3
        assert total_duration < 2.0, f"Workflow took too long: {total_duration}s"

    finally:
        await imap_handler.close_all()
        await smtp_handler.close()
        await imap_pool.close()
        await smtp_pool.close()

@pytest.mark.asyncio
async def test_respects_gmail_rate_limits_mock():
    """
    Verify that the system handles rate limit rejections correctly.
    Simulates sending 500 emails and getting a rejection on the 501st.
    """
    from src.v3_smtp_redis_pool import RedisSMTPPool, HybridSMTPHandler
    import aiosmtplib

    # Mock the SMTP pool and connection
    pool_mock = MagicMock(spec=RedisSMTPPool)
    pool_mock.get_session = AsyncMock(return_value=None)
    pool_mock.store_session = AsyncMock()
    pool_mock.refresh_ttl = AsyncMock()
    pool_mock.stats = {"created": 0}

    # Setup the handler with a mock SMTP client
    creds = {"user": "me@gmail.com", "password": "pw", "host": "smtp.gmail.com"}
    handler = HybridSMTPHandler(pool_mock, creds)

    # Convert the SMTP class to a mock that counts calls
    with patch("aiosmtplib.SMTP") as mock_smtp_cls:
        # Create a mock instance
        mock_instance = AsyncMock()
        mock_smtp_cls.return_value = mock_instance

        # Configure connection methods
        mock_instance.connect = AsyncMock()
        mock_instance.login = AsyncMock()
        mock_instance.starttls = AsyncMock()
        mock_instance.quit = AsyncMock()

        # Configure send_message with side effect
        call_count = 0
        
        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count > 500:
                # Simulate Gmail's "4.4.2 ... limit exceeded" or similar
                raise aiosmtplib.SMTPResponseException(452, "4.5.3 Domain policy violated")
            return ({}, "OK")

        mock_instance.send_message = AsyncMock(side_effect=side_effect)

        # Run the 'attack' loop
        # In reality, we shouldn't send 500 sequentially in a unit test if it takes time.
        # But with mocks, it's instant.
        
        responses = []
        try:
            for i in range(501):
                try:
                    await handler.send_message("to@ex.com", f"Subj {i}", "Body")
                    responses.append("OK")
                except aiosmtplib.SMTPResponseException as e:
                    responses.append(f"ERROR: {e.code}")
                    break
        finally:
            await handler.close()

        # Assertions
        assert len(responses) == 501
        assert responses[-1] == "ERROR: 452"
        assert call_count == 501
        print(f"\n[RATE LIMIT] Successfully processed {len(responses)-1} messages, rejected 501st.")
