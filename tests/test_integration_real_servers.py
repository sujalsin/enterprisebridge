"""Integration tests with real IMAP/SMTP servers.

These tests connect to actual Gmail servers to verify the stateless handlers work correctly.
They also demonstrate the latency overhead of creating fresh connections.

Run with: pytest tests/test_integration_real_servers.py -v -s
"""

import os
import asyncio
import time

import pytest
from dotenv import load_dotenv

from src.v1_imap_stateless import StatelessIMAPHandler
from src.v1_smtp_stateless import StatelessSMTPHandler

# Load environment variables
load_dotenv()


@pytest.fixture
def gmail_imap_creds():
    """Real Gmail IMAP credentials from .env."""
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    
    if not email or not password:
        pytest.skip("TEST_GMAIL_EMAIL and TEST_GMAIL_PASSWORD required in .env")
    
    return {
        "host": "imap.gmail.com",
        "user": email,
        "password": password,
    }


@pytest.fixture
def gmail_smtp_creds():
    """Real Gmail SMTP credentials from .env."""
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    
    if not email or not password:
        pytest.skip("TEST_GMAIL_EMAIL and TEST_GMAIL_PASSWORD required in .env")
    
    return {
        "host": "smtp.gmail.com",
        "port": 587,
        "user": email,
        "password": password,
        "use_tls": False,  # We'll use STARTTLS
    }


class TestRealIMAPConnection:
    """Integration tests for IMAP with real Gmail server."""

    @pytest.mark.asyncio
    async def test_real_imap_fetch_messages(self, gmail_imap_creds):
        """Test fetching messages from real Gmail INBOX."""
        handler = StatelessIMAPHandler(gmail_imap_creds)
        
        start = time.perf_counter()
        messages = await handler.fetch_messages(folder="INBOX", limit=3)
        elapsed = time.perf_counter() - start
        
        print(f"\n[IMAP] Fetched {len(messages)} messages in {elapsed:.2f}s")
        
        # Should return messages (or empty if inbox is empty)
        assert isinstance(messages, list)
        
        # Log the latency - this demonstrates stateless overhead
        print(f"[IMAP] Connection overhead visible: {elapsed:.2f}s")

    @pytest.mark.asyncio
    async def test_real_imap_multiple_calls_latency(self, gmail_imap_creds):
        """
        Demonstrate stateless overhead for multiple IMAP calls.
        
        NOTE: The OS caches TLS sessions, so after the first cold call,
        subsequent calls benefit from TLS session resumption. Each call still:
        - Creates a new TCP connection
        - Performs IMAP LOGIN
        - Executes the full query
        - Disconnects (LOGOUT)
        
        This measures the realistic overhead of stateless connections.
        """
        times = []
        for i in range(3):
            # Create a NEW handler for each call - simulates separate requests
            handler = StatelessIMAPHandler(gmail_imap_creds)
            
            start = time.perf_counter()
            await handler.fetch_messages(folder="INBOX", limit=1)
            elapsed = time.perf_counter() - start
            times.append(elapsed)
            print(f"\n[IMAP] Call {i+1}: {elapsed:.2f}s")
        
        total_time = sum(times)
        avg_time = total_time / len(times)
        
        print(f"\n[IMAP] Total for 3 calls: {total_time:.2f}s")
        print(f"[IMAP] Average per call: {avg_time:.2f}s")
        print(f"[IMAP] NOTE: With connection pooling, 3 calls could be <0.3s total")
        
        # Each stateless call still has TCP + login overhead (even with TLS resume)
        assert avg_time > 0.05, f"Expected >50ms per call, got {avg_time*1000:.0f}ms"
        assert total_time > 0.15, f"Expected >150ms total, got {total_time*1000:.0f}ms"


class TestRealSMTPConnection:
    """Integration tests for SMTP with real Gmail server."""

    @pytest.mark.asyncio
    async def test_real_smtp_send_message(self, gmail_smtp_creds):
        """Test sending a message through real Gmail SMTP."""
        handler = StatelessSMTPHandler(gmail_smtp_creds)
        
        test_email = gmail_smtp_creds["user"]
        
        start = time.perf_counter()
        result = await handler.send_message(
            to=test_email,
            subject=f"[AgentMail Test] {time.strftime('%Y-%m-%d %H:%M:%S')}",
            body="This is a test email from the AgentMail stateless SMTP handler.",
        )
        elapsed = time.perf_counter() - start
        
        print(f"\n[SMTP] Sent message in {elapsed:.2f}s")
        print(f"[SMTP] Result: {result}")
        
        assert result["status"] == "sent"
        print(f"[SMTP] Connection overhead visible: {elapsed:.2f}s")

    @pytest.mark.asyncio
    async def test_real_smtp_multiple_sends_latency(self, gmail_smtp_creds):
        """Demonstrate that each send has full connection overhead."""
        handler = StatelessSMTPHandler(gmail_smtp_creds)
        
        test_email = gmail_smtp_creds["user"]
        
        times = []
        for i in range(3):
            start = time.perf_counter()
            await handler.send_message(
                to=test_email,
                subject=f"[AgentMail Test {i+1}] {time.strftime('%H:%M:%S')}",
                body=f"Test message {i+1}",
            )
            elapsed = time.perf_counter() - start
            times.append(elapsed)
            print(f"\n[SMTP] Send {i+1}: {elapsed:.2f}s")
        
        avg_time = sum(times) / len(times)
        print(f"\n[SMTP] Average per send: {avg_time:.2f}s")
        print(f"[SMTP] Total for 3 sends: {sum(times):.2f}s")
        
        # Each send should take significant time (connection overhead)
        assert avg_time > 0.3, f"Expected >0.3s per send, got {avg_time:.2f}s"


class TestCombinedLatencyBenchmark:
    """Benchmark comparing stateless overhead."""

    @pytest.mark.asyncio
    async def test_stateless_total_overhead(self, gmail_imap_creds, gmail_smtp_creds):
        """
        Benchmark total overhead for a read-then-reply workflow.
        
        This simulates: fetch 1 message, send 1 reply.
        With stateless handlers, this requires 2 full connection cycles.
        """
        imap_handler = StatelessIMAPHandler(gmail_imap_creds)
        smtp_handler = StatelessSMTPHandler(gmail_smtp_creds)
        
        test_email = gmail_smtp_creds["user"]
        
        # Simulate read-then-reply workflow
        total_start = time.perf_counter()
        
        # Step 1: Fetch message
        fetch_start = time.perf_counter()
        messages = await imap_handler.fetch_messages(folder="INBOX", limit=1)
        fetch_time = time.perf_counter() - fetch_start
        
        # Step 2: Send reply
        send_start = time.perf_counter()
        await smtp_handler.send_message(
            to=test_email,
            subject="[AgentMail] Workflow Test Reply",
            body="This is an automated reply.",
        )
        send_time = time.perf_counter() - send_start
        
        total_time = time.perf_counter() - total_start
        
        print(f"\n{'='*50}")
        print(f"STATELESS OVERHEAD BENCHMARK")
        print(f"{'='*50}")
        print(f"IMAP Fetch:  {fetch_time:.2f}s")
        print(f"SMTP Send:   {send_time:.2f}s")
        print(f"{'='*50}")
        print(f"TOTAL:       {total_time:.2f}s")
        print(f"{'='*50}")
        print(f"\nWith connection pooling, this could be <0.5s total!")
