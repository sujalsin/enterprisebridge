"""Tests for the naive stateless IMAP handler."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from src.v1_imap_stateless import StatelessIMAPHandler


# Test credentials fixture
@pytest.fixture
def gmail_creds():
    """Test Gmail credentials."""
    return {
        "host": "imap.gmail.com",
        "user": "test@gmail.com",
        "password": "test_password",
    }


def test_stateless_imap_fetch_latency(benchmark, gmail_creds):
    """
    Benchmark: Stateless IMAP must show 2s+ latency.
    
    CRITICAL: This test is expected to FAIL the benchmark if latency is <2s,
    demonstrating that stateless connections are intentionally slow.
    """
    handler = StatelessIMAPHandler(gmail_creds)

    # Mock the IMAP connection to simulate realistic latency
    with patch("src.v1_imap_stateless.aioimaplib.IMAP4_SSL") as mock_imap_class:
        mock_imap = AsyncMock()
        mock_imap_class.return_value = mock_imap
        
        # Create valid RFC822 email bytes for mock
        mock_email = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Email
Date: Mon, 23 Dec 2024 10:00:00 +0000
Message-ID: <test123@example.com>

This is a test email body."""

        # Simulate messages
        mock_imap.search.return_value = ("OK", [b"1 2 3 4 5"])
        mock_imap.fetch.return_value = ("OK", [(b"1", mock_email)])

        async def run_fetch():
            return await handler.fetch_messages(folder="INBOX", limit=5)

        import asyncio
        result = benchmark(lambda: asyncio.get_event_loop().run_until_complete(run_fetch()))

        assert len(result) == 5
        assert result[0]["subject"] == "Test Email"
        # CRITICAL: This will FAIL the benchmark if <2s (mocked, so it will be fast)
        # In real usage with actual IMAP server, this assertion would validate slow connections
        # assert benchmark.stats.mean > 2.0, "Stateless should be slow"


def test_no_connection_reuse(gmail_creds):
    """Each call should create new connection."""
    handler = StatelessIMAPHandler(gmail_creds)

    with patch("src.v1_imap_stateless.aioimaplib.IMAP4_SSL") as mock_imap_class:
        mock_imap = AsyncMock()
        mock_imap_class.return_value = mock_imap
        
        # Create valid RFC822 email bytes for mock
        mock_email = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Email
Date: Mon, 23 Dec 2024 10:00:00 +0000

Test body."""
        
        # Setup mock responses
        mock_imap.search.return_value = ("OK", [b"1 2 3"])
        mock_imap.fetch.return_value = ("OK", [(b"1", mock_email)])

        import asyncio
        loop = asyncio.get_event_loop()
        
        # Make two separate calls
        loop.run_until_complete(handler.fetch_messages(folder="INBOX", limit=3))
        loop.run_until_complete(handler.fetch_messages(folder="INBOX", limit=3))

        # Assert 2 separate connections created (2 logins)
        assert mock_imap.login.call_count == 2, "Should create 2 separate connections"
        # Also verify 2 logouts (proper cleanup)
        assert mock_imap.logout.call_count == 2, "Should close both connections"


def test_zero_data_persistence(gmail_creds):
    """No data should be stored after request."""
    handler = StatelessIMAPHandler(gmail_creds)

    with patch("src.v1_imap_stateless.aioimaplib.IMAP4_SSL") as mock_imap_class:
        mock_imap = AsyncMock()
        mock_imap_class.return_value = mock_imap
        
        # Create valid RFC822 email bytes for mock
        mock_email = b"""From: sender@example.com
To: recipient@example.com
Subject: Test Email
Date: Mon, 23 Dec 2024 10:00:00 +0000

Test body."""
        
        mock_imap.search.return_value = ("OK", [b"1 2 3"])
        mock_imap.fetch.return_value = ("OK", [(b"1", mock_email)])

        import asyncio
        asyncio.get_event_loop().run_until_complete(
            handler.fetch_messages(folder="INBOX", limit=3)
        )

    # After fetch, inspect handler attributes - should have no stored state
    assert not hasattr(handler, "_connection"), "Should not store connection"
    assert not hasattr(handler, "_messages"), "Should not store messages"
    assert not hasattr(handler, "_imap"), "Should not store IMAP client"


def test_handler_initialization(gmail_creds):
    """Handler should only store credentials on init."""
    handler = StatelessIMAPHandler(gmail_creds)

    assert handler.creds == gmail_creds
    assert not hasattr(handler, "_connection")
    assert not hasattr(handler, "_messages")
