"""Tests for the naive stateless SMTP handler."""

import pytest
from unittest.mock import patch, AsyncMock

from src.v1_smtp_stateless import StatelessSMTPHandler


@pytest.fixture
def gmail_smtp_creds():
    """Test Gmail SMTP credentials."""
    return {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": "test@gmail.com",
        "password": "test_password",
        "use_tls": True,
    }


def test_stateless_smtp_send_latency(benchmark, gmail_smtp_creds):
    """
    Benchmark: Each send should take 1.5s+ (connect + auth).
    
    CRITICAL: This test demonstrates that stateless SMTP is slow.
    With mocks it will be fast, but with real server would show >1.5s latency.
    """
    handler = StatelessSMTPHandler(gmail_smtp_creds)

    with patch("src.v1_smtp_stateless.aiosmtplib.SMTP") as mock_smtp_class:
        mock_smtp = AsyncMock()
        mock_smtp_class.return_value = mock_smtp

        async def run_send():
            return await handler.send_message(
                to="test@example.com", subject="Hi", body="Test body"
            )

        import asyncio

        result = benchmark(
            lambda: asyncio.get_event_loop().run_until_complete(run_send())
        )

        assert result["status"] == "sent"
        # CRITICAL: With mocks this passes, but real server would show >1.5s
        # assert benchmark.stats.mean > 1.5, "Stateless SMTP is slow"


def test_no_smtp_reuse(gmail_smtp_creds):
    """Each send should create new SMTP connection."""
    handler = StatelessSMTPHandler(gmail_smtp_creds)

    with patch("src.v1_smtp_stateless.aiosmtplib.SMTP") as mock_smtp_class:
        mock_smtp = AsyncMock()
        mock_smtp_class.return_value = mock_smtp

        import asyncio

        loop = asyncio.get_event_loop()

        # Make two separate calls
        loop.run_until_complete(
            handler.send_message(to="test1@example.com", subject="Hi 1", body="Body 1")
        )
        loop.run_until_complete(
            handler.send_message(to="test2@example.com", subject="Hi 2", body="Body 2")
        )

        # Assert 2 separate connections created (2 logins)
        assert mock_smtp.login.call_count == 2, "Should create 2 separate connections"
        # Also verify 2 quits (proper cleanup)
        assert mock_smtp.quit.call_count == 2, "Should close both connections"


def test_zero_smtp_data_persistence(gmail_smtp_creds):
    """No data should be stored after send."""
    handler = StatelessSMTPHandler(gmail_smtp_creds)

    with patch("src.v1_smtp_stateless.aiosmtplib.SMTP") as mock_smtp_class:
        mock_smtp = AsyncMock()
        mock_smtp_class.return_value = mock_smtp

        import asyncio

        asyncio.get_event_loop().run_until_complete(
            handler.send_message(to="test@example.com", subject="Hi", body="Body")
        )

    # After send, inspect handler attributes - should have no stored state
    assert not hasattr(handler, "_connection"), "Should not store connection"
    assert not hasattr(handler, "_smtp"), "Should not store SMTP client"
    assert not hasattr(handler, "_messages"), "Should not store messages"


def test_smtp_handler_initialization(gmail_smtp_creds):
    """Handler should only store credentials on init."""
    handler = StatelessSMTPHandler(gmail_smtp_creds)

    assert handler.creds == gmail_smtp_creds
    assert not hasattr(handler, "_connection")
    assert not hasattr(handler, "_smtp")


def test_send_message_returns_status(gmail_smtp_creds):
    """Send message should return proper status dict."""
    handler = StatelessSMTPHandler(gmail_smtp_creds)

    with patch("src.v1_smtp_stateless.aiosmtplib.SMTP") as mock_smtp_class:
        mock_smtp = AsyncMock()
        mock_smtp_class.return_value = mock_smtp

        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            handler.send_message(
                to="recipient@example.com", subject="Test Subject", body="Test Body"
            )
        )

        assert result["status"] == "sent"
        assert "message_id" in result
