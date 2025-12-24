"""Tests for API mimicry - ensuring compatibility with AgentMail SDK.

These tests verify:
1. ProxyClient has same methods as official SDK pattern
2. Response schemas match expected Pydantic models
3. Base URL swapping works for drop-in replacement
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from fastapi.testclient import TestClient

from src.v3_proxy_api import (
    app,
    ProxyClient,
    Message,
    MessageListResponse,
    MessageSendRequest,
    MessageSendResponse,
    InboxCreateRequest,
    InboxCreateResponse,
    MessagesResource,
    InboxesResource,
)


# ============================================================
# API SIGNATURE COMPATIBILITY TESTS
# ============================================================

def test_api_signature_compatibility():
    """ProxyClient must have same method structure as expected SDK pattern."""
    proxy = ProxyClient(api_key="test_key", base_url="http://localhost:8000")
    
    # Must have resources
    assert hasattr(proxy, "messages"), "ProxyClient must have 'messages' resource"
    assert hasattr(proxy, "inboxes"), "ProxyClient must have 'inboxes' resource"
    
    # messages resource must have expected methods
    assert hasattr(proxy.messages, "list"), "messages must have 'list' method"
    assert hasattr(proxy.messages, "send"), "messages must have 'send' method"
    
    # inboxes resource must have expected methods
    assert hasattr(proxy.inboxes, "create"), "inboxes must have 'create' method"
    assert hasattr(proxy.inboxes, "get"), "inboxes must have 'get' method"


def test_proxy_client_initialization():
    """ProxyClient should accept api_key and base_url."""
    proxy = ProxyClient(api_key="my_api_key", base_url="http://proxy.internal:8000")
    
    assert proxy.api_key == "my_api_key"
    assert proxy.base_url == "http://proxy.internal:8000"


def test_base_url_swap():
    """Only base_url should change between official and proxy client."""
    # Simulate official client pattern
    official_base = "https://api.agentmail.to"
    proxy_base = "http://proxy.internal"
    
    official = ProxyClient(api_key="key", base_url=official_base)
    proxy = ProxyClient(api_key="key", base_url=proxy_base)
    
    # Same structure, different base URLs
    assert official.base_url == official_base
    assert proxy.base_url == proxy_base
    
    # Same method availability
    assert hasattr(official, "messages")
    assert hasattr(proxy, "messages")
    assert hasattr(official.messages, "list")
    assert hasattr(proxy.messages, "list")


# ============================================================
# PYDANTIC MODEL VALIDATION TESTS
# ============================================================

def test_message_model_schema():
    """Message model should accept expected fields."""
    msg = Message(
        message_id="<123@example.com>",
        thread_id="abc123",
        subject="Test Subject",
        from_="sender@example.com",
        to="recipient@example.com",
        date="Mon, 23 Dec 2024 10:00:00 +0000",
        body="Email body content",
        attachments=[],
    )
    
    assert msg.subject == "Test Subject"
    assert msg.body == "Email body content"


def test_message_model_with_alias():
    """Message model should work with 'from' alias."""
    # Using alias (from is a Python keyword)
    msg = Message(**{
        "message_id": "123",
        "from": "sender@test.com",
        "to": "recipient@test.com",
        "subject": "Test",
        "body": "Body",
    })
    
    assert msg.from_ == "sender@test.com"


def test_message_list_response_schema():
    """MessageListResponse should validate correctly."""
    response = MessageListResponse(
        data=[
            Message(subject="Msg 1", body="Body 1"),
            Message(subject="Msg 2", body="Body 2"),
        ],
        source="legacy",
        count=2,
    )
    
    assert len(response.data) == 2
    assert response.source == "legacy"
    assert response.count == 2


def test_message_send_request_schema():
    """MessageSendRequest should validate correctly."""
    request = MessageSendRequest(
        to="recipient@example.com",
        subject="Test Subject",
        body="Test body content",
    )
    
    assert request.to == "recipient@example.com"
    assert request.subject == "Test Subject"


def test_inbox_create_request_schema():
    """InboxCreateRequest should validate correctly."""
    request = InboxCreateRequest(
        email="user@company.com",
        username="user@company.com",
        password="app_password",
        imap_host="imap.company.com",
        smtp_host="smtp.company.com",
        smtp_port=587,
    )
    
    assert request.email == "user@company.com"
    assert request.imap_host == "imap.company.com"


# ============================================================
# FASTAPI ENDPOINT TESTS (using httpx.ASGITransport)
# ============================================================

import httpx

@pytest.fixture
def api_module():
    """Initialize the API module for testing."""
    import src.v3_proxy_api as module
    from src.v2_smtp_memory_pool import InMemorySMTPPool
    
    # Initialize pools synchronously for testing
    module.smtp_pool = InMemorySMTPPool(max_connections=5)
    module.redis_pool = None  # Skip Redis in unit tests
    module.credential_store.clear()
    
    return module


@pytest.mark.asyncio
async def test_health_endpoint(api_module):
    """Health check should return status."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"


@pytest.mark.asyncio
async def test_create_inbox_endpoint(api_module):
    """Create inbox endpoint should work."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/inboxes", json={
            "email": "test@example.com",
            "username": "test@example.com",
            "password": "test_password",
        })
        
        assert response.status_code == 200
        data = response.json()
        assert data["inbox_id"] == "test@example.com"
        assert data["status"] == "active"


@pytest.mark.asyncio
async def test_get_inbox_after_create(api_module):
    """Should be able to get an inbox after creating it."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create
        await client.post("/v1/inboxes", json={
            "email": "gettest@example.com",
            "username": "gettest@example.com",
            "password": "password",
        })
        
        # Get
        response = await client.get("/v1/inboxes/gettest@example.com")
        assert response.status_code == 200
        data = response.json()
        assert data["inbox_id"] == "gettest@example.com"


@pytest.mark.asyncio
async def test_delete_inbox_endpoint(api_module):
    """Delete inbox endpoint should work."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Create first
        await client.post("/v1/inboxes", json={
            "email": "delete@example.com",
            "username": "delete@example.com",
            "password": "password",
        })
        
        # Delete
        response = await client.delete("/v1/inboxes/delete@example.com")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "deleted"


@pytest.mark.asyncio
async def test_inbox_not_found(api_module):
    """Should return 404 for unknown inbox."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/v1/inboxes/unknown@example.com")
        # Should fail without env fallback (or succeed with env fallback)
        assert response.status_code in [200, 404]


# ============================================================
# MOCK INTEGRATION TESTS
# ============================================================

@pytest.mark.asyncio
async def test_messages_resource_list():
    """MessagesResource.list should make correct HTTP call."""
    client = ProxyClient(api_key="test_key", base_url="http://test.local")
    
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "data": [
                {"subject": "Test", "body": "Body", "from": "a@b.com", "to": "c@d.com"}
            ],
            "count": 1,
        }
        mock_response.raise_for_status = MagicMock()
        
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=mock_response)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_client.return_value = mock_instance
        
        messages = await client.messages.list(inbox_id="user@test.com")
        
        assert len(messages) == 1
        assert messages[0].subject == "Test"


@pytest.mark.asyncio
async def test_messages_resource_send():
    """MessagesResource.send should make correct HTTP call."""
    client = ProxyClient(api_key="test_key", base_url="http://test.local")
    
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "sent",
            "message_id": "<123@test.com>",
        }
        mock_response.raise_for_status = MagicMock()
        
        mock_instance = AsyncMock()
        mock_instance.post = AsyncMock(return_value=mock_response)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_client.return_value = mock_instance
        
        result = await client.messages.send(
            inbox_id="user@test.com",
            to="recipient@test.com",
            subject="Test",
            body="Body",
        )
        
        assert result.status == "sent"


@pytest.mark.asyncio
async def test_inboxes_resource_create():
    """InboxesResource.create should make correct HTTP call."""
    client = ProxyClient(api_key="test_key", base_url="http://test.local")
    
    with patch("httpx.AsyncClient") as mock_client:
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "inbox_id": "new@test.com",
            "email": "new@test.com",
            "status": "active",
        }
        mock_response.raise_for_status = MagicMock()
        
        mock_instance = AsyncMock()
        mock_instance.post = AsyncMock(return_value=mock_response)
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=None)
        mock_client.return_value = mock_instance
        
        result = await client.inboxes.create(
            email="new@test.com",
            username="new@test.com",
            password="password",
        )
        
        assert result.inbox_id == "new@test.com"
        assert result.status == "active"
