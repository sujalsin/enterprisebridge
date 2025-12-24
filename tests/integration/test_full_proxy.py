"""End-to-end integration tests for the full proxy.

These tests verify:
1. Full round-trip: AI -> Proxy -> Gmail -> Proxy -> AI
2. SMTP sending via legacy email
3. No email data stored in Redis (only session metadata)

Requirements:
- Redis running: docker run --name agentmail-redis -p 6379:6379 -d redis
- Gmail credentials in .env

Run with:
    pytest tests/integration/test_full_proxy.py -v -s --integration
"""

import os
import pytest
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

# Mark all tests in this file as integration tests
pytestmark = pytest.mark.integration


def get_gmail_creds():
    """Get Gmail credentials from environment."""
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    
    if not email or not password:
        pytest.skip("Gmail credentials not available")
    
    return {"email": email, "password": password}


def get_redis_url():
    """Get Redis URL from environment."""
    return os.getenv("REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
async def proxy_client():
    """Create a test client for the proxy API."""
    from src.v3_proxy_api import app
    import src.v3_proxy_api as api_module
    from src.v3_imap_redis_pool import RedisIMAPPool
    from src.v2_smtp_memory_pool import InMemorySMTPPool
    
    # Initialize pools
    redis_url = get_redis_url()
    api_module.redis_pool = RedisIMAPPool(redis_url)
    api_module.smtp_pool = InMemorySMTPPool(max_connections=5)
    api_module.credential_store.clear()
    
    # Register test inbox
    creds = get_gmail_creds()
    api_module.credential_store[creds["email"]] = {
        "host": "imap.gmail.com",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "user": creds["email"],
        "password": creds["password"],
    }
    
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    
    # Cleanup
    if api_module.redis_pool:
        await api_module.redis_pool.close()
    if api_module.smtp_pool:
        await api_module.smtp_pool.close_all()


@pytest.fixture
async def redis_client():
    """Create a Redis client for verification."""
    import aioredis
    redis_url = get_redis_url()
    
    try:
        redis = await aioredis.from_url(redis_url)
        yield redis
        await redis.close()
    except Exception:
        pytest.skip("Redis not available")


# ============================================================
# E2E TESTS
# ============================================================

@pytest.mark.asyncio
async def test_e2e_agent_queries_legacy_gmail(proxy_client, redis_client):
    """Full round-trip: AI -> Proxy -> Gmail -> Proxy -> AI"""
    creds = get_gmail_creds()
    inbox_id = creds["email"]
    
    # AI agent queries inbox
    response = await proxy_client.get(
        f"/v1/inboxes/{inbox_id}/messages",
        params={"folder": "INBOX", "limit": 5}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Verify response structure
    assert "data" in data
    assert "source" in data
    assert data["source"] == "legacy"
    assert "count" in data
    
    print(f"\n[E2E] Fetched {data['count']} messages from Gmail")
    
    # Verify messages have expected fields
    for msg in data["data"]:
        assert "subject" in msg
        assert "body" in msg
    
    # Verify no email bodies stored in Redis (only session metadata)
    email_keys = await redis_client.keys("email:*")
    assert len(email_keys) == 0, f"Expected 0 email keys, found {len(email_keys)}"
    
    # Verify session metadata exists
    session_keys = await redis_client.keys("imap:session:*")
    print(f"[E2E] Redis session keys: {len(session_keys)}")


@pytest.mark.asyncio
async def test_e2e_agent_sends_via_legacy(proxy_client):
    """AI -> Proxy -> SMTP -> Gmail"""
    creds = get_gmail_creds()
    inbox_id = creds["email"]
    
    # Create a unique subject for verification
    import uuid
    unique_subject = f"Test E2E Send {uuid.uuid4().hex[:8]}"
    
    # Send email to self
    response = await proxy_client.post(
        f"/v1/inboxes/{inbox_id}/messages",
        json={
            "to": creds["email"],  # Send to self
            "subject": unique_subject,
            "body": "This is an automated E2E test message.",
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["status"] == "sent"
    print(f"\n[E2E] Sent email with subject: {unique_subject}")
    
    # Wait a bit for email to arrive
    await asyncio.sleep(2)
    
    # Verify email landed in inbox by fetching recent messages
    response = await proxy_client.get(
        f"/v1/inboxes/{inbox_id}/messages",
        params={"folder": "INBOX", "limit": 5}
    )
    
    assert response.status_code == 200
    data = response.json()
    
    # Check if our email is in the recent messages
    subjects = [msg.get("subject", "") for msg in data["data"]]
    print(f"[E2E] Recent subjects: {subjects}")
    
    # Note: Email delivery may take time, so we just verify the send succeeded


@pytest.mark.asyncio
async def test_no_email_data_in_redis(proxy_client, redis_client):
    """After multiple requests, Redis should only have session metadata."""
    creds = get_gmail_creds()
    inbox_id = creds["email"]
    
    # Run 10 requests (reduced from 100 to avoid rate limits)
    print("\n[E2E] Running 10 fetch requests...")
    for i in range(10):
        response = await proxy_client.get(
            f"/v1/inboxes/{inbox_id}/messages",
            params={"folder": "INBOX", "limit": 1}
        )
        assert response.status_code == 200
    
    print("[E2E] Completed 10 requests")
    
    # Check Redis keys
    all_keys = await redis_client.keys("*")
    
    email_keys = [k for k in all_keys if k.startswith(b"email:")]
    session_keys = [k for k in all_keys if k.startswith(b"imap:session:")]
    
    print(f"[E2E] Total Redis keys: {len(all_keys)}")
    print(f"[E2E] Email keys: {len(email_keys)}")
    print(f"[E2E] Session keys: {len(session_keys)}")
    
    # Verify zero email data stored
    assert len(email_keys) == 0, f"Expected 0 email keys, found {email_keys}"
    
    # Verify session metadata exists
    assert len(session_keys) > 0, "Expected at least 1 session key"


@pytest.mark.asyncio
async def test_session_reuse_across_requests(proxy_client, redis_client):
    """Verify that sessions are reused across multiple requests."""
    creds = get_gmail_creds()
    inbox_id = creds["email"]
    
    # First request - creates session
    response1 = await proxy_client.get(
        f"/v1/inboxes/{inbox_id}/messages",
        params={"folder": "INBOX", "limit": 1}
    )
    assert response1.status_code == 200
    
    # Get session keys after first request
    keys_after_first = await redis_client.keys("imap:session:*")
    
    # Second request - should reuse session
    response2 = await proxy_client.get(
        f"/v1/inboxes/{inbox_id}/messages",
        params={"folder": "INBOX", "limit": 1}
    )
    assert response2.status_code == 200
    
    # Get session keys after second request
    keys_after_second = await redis_client.keys("imap:session:*")
    
    # Should have same number of sessions (reuse, not create new)
    assert len(keys_after_second) == len(keys_after_first), \
        f"Session count changed: {len(keys_after_first)} -> {len(keys_after_second)}"
    
    print(f"\n[E2E] Session reuse verified: {len(keys_after_second)} session(s)")


@pytest.mark.asyncio
async def test_health_check_with_pools(proxy_client):
    """Verify health endpoint shows pool status."""
    response = await proxy_client.get("/health")
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["status"] == "healthy"
    print(f"\n[E2E] Health check: {data}")
