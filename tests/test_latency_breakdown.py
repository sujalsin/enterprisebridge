"""Application-layer latency benchmarks for stateless handlers.

These tests measure where time is spent at the application layer:
- Connect (TCP + TLS handshake)
- Login/Auth
- Operation (fetch/send)
- Disconnect

Run with: pytest tests/test_latency_breakdown.py -v -s
"""

import os
import asyncio

import pytest
from dotenv import load_dotenv

from src.v1_imap_stateless import StatelessIMAPHandler
from src.v1_smtp_stateless import StatelessSMTPHandler

load_dotenv()


@pytest.fixture
def gmail_imap_creds():
    """Real Gmail IMAP credentials from .env."""
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    if not email or not password:
        pytest.skip("TEST_GMAIL_EMAIL and TEST_GMAIL_PASSWORD required")
    return {"host": "imap.gmail.com", "user": email, "password": password}


@pytest.fixture
def gmail_smtp_creds():
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


class TestIMAPLatencyBreakdown:
    """Measure application-layer latency for IMAP operations."""

    @pytest.mark.asyncio
    async def test_imap_latency_breakdown(self, gmail_imap_creds):
        """Measure time spent in each IMAP phase."""
        handler = StatelessIMAPHandler(gmail_imap_creds)
        
        result = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        timing = result["timing"]
        
        print("\n" + "=" * 60)
        print("IMAP APPLICATION-LAYER LATENCY BREAKDOWN")
        print("=" * 60)
        print(f"  Connect (TCP+TLS):  {timing['connect_ms']:>8.1f} ms")
        print(f"  Login:              {timing['login_ms']:>8.1f} ms")
        print(f"  Select folder:      {timing['select_ms']:>8.1f} ms")
        print(f"  Search:             {timing['search_ms']:>8.1f} ms")
        print(f"  Fetch messages:     {timing['fetch_ms']:>8.1f} ms")
        print(f"  Logout:             {timing['logout_ms']:>8.1f} ms")
        print("-" * 60)
        print(f"  TOTAL:              {timing['total_ms']:>8.1f} ms")
        print("=" * 60)
        
        # Calculate overhead vs operation
        overhead = timing["connect_ms"] + timing["login_ms"] + timing["logout_ms"]
        operation = timing["select_ms"] + timing["search_ms"] + timing["fetch_ms"]
        
        print(f"\n  Connection overhead: {overhead:.1f} ms ({overhead/timing['total_ms']*100:.0f}%)")
        print(f"  Actual operation:    {operation:.1f} ms ({operation/timing['total_ms']*100:.0f}%)")
        
        # The overhead should be measurable
        assert timing["connect_ms"] > 0
        assert timing["login_ms"] > 0
        assert timing["total_ms"] > 0

    @pytest.mark.asyncio
    async def test_imap_multiple_calls_overhead(self, gmail_imap_creds):
        """Measure cumulative overhead across multiple stateless calls."""
        total_overhead = 0
        total_operation = 0
        
        print("\n" + "=" * 60)
        print("IMAP CUMULATIVE OVERHEAD (3 CALLS)")
        print("=" * 60)
        
        for i in range(3):
            handler = StatelessIMAPHandler(gmail_imap_creds)
            result = await handler.fetch_messages_instrumented(folder="INBOX", limit=1)
            timing = result["timing"]
            
            overhead = timing["connect_ms"] + timing["login_ms"] + timing["logout_ms"]
            operation = timing["select_ms"] + timing["search_ms"] + timing["fetch_ms"]
            
            total_overhead += overhead
            total_operation += operation
            
            print(f"  Call {i+1}: overhead={overhead:.0f}ms, operation={operation:.0f}ms")
        
        print("-" * 60)
        print(f"  TOTAL OVERHEAD:    {total_overhead:.0f} ms")
        print(f"  TOTAL OPERATION:   {total_operation:.0f} ms")
        print(f"  WASTED ON CONNECT: {total_overhead/(total_overhead+total_operation)*100:.0f}%")
        print("=" * 60)


class TestSMTPLatencyBreakdown:
    """Measure application-layer latency for SMTP operations."""

    @pytest.mark.asyncio
    async def test_smtp_latency_breakdown(self, gmail_smtp_creds):
        """Measure time spent in each SMTP phase."""
        handler = StatelessSMTPHandler(gmail_smtp_creds)
        test_email = gmail_smtp_creds["user"]
        
        result = await handler.send_message_instrumented(
            to=test_email,
            subject="[Latency Test] Phase breakdown",
            body="Testing application-layer latency.",
        )
        timing = result["timing"]
        
        print("\n" + "=" * 60)
        print("SMTP APPLICATION-LAYER LATENCY BREAKDOWN")
        print("=" * 60)
        print(f"  Connect (TCP+TLS):  {timing['connect_ms']:>8.1f} ms")
        print(f"  Login:              {timing['login_ms']:>8.1f} ms")
        print(f"  Send message:       {timing['send_ms']:>8.1f} ms")
        print(f"  Quit:               {timing['quit_ms']:>8.1f} ms")
        print("-" * 60)
        print(f"  TOTAL:              {timing['total_ms']:>8.1f} ms")
        print("=" * 60)
        
        # Calculate overhead vs operation
        overhead = timing["connect_ms"] + timing["login_ms"] + timing["quit_ms"]
        operation = timing["send_ms"]
        
        print(f"\n  Connection overhead: {overhead:.1f} ms ({overhead/timing['total_ms']*100:.0f}%)")
        print(f"  Actual send:         {operation:.1f} ms ({operation/timing['total_ms']*100:.0f}%)")

    @pytest.mark.asyncio
    async def test_smtp_multiple_sends_overhead(self, gmail_smtp_creds):
        """Measure cumulative overhead across multiple stateless sends."""
        total_overhead = 0
        total_operation = 0
        test_email = gmail_smtp_creds["user"]
        
        print("\n" + "=" * 60)
        print("SMTP CUMULATIVE OVERHEAD (3 SENDS)")
        print("=" * 60)
        
        for i in range(3):
            handler = StatelessSMTPHandler(gmail_smtp_creds)
            result = await handler.send_message_instrumented(
                to=test_email,
                subject=f"[Latency Test {i+1}]",
                body=f"Message {i+1}",
            )
            timing = result["timing"]
            
            overhead = timing["connect_ms"] + timing["login_ms"] + timing["quit_ms"]
            operation = timing["send_ms"]
            
            total_overhead += overhead
            total_operation += operation
            
            print(f"  Send {i+1}: overhead={overhead:.0f}ms, operation={operation:.0f}ms")
        
        print("-" * 60)
        print(f"  TOTAL OVERHEAD:    {total_overhead:.0f} ms")
        print(f"  TOTAL OPERATION:   {total_operation:.0f} ms")
        print(f"  WASTED ON CONNECT: {total_overhead/(total_overhead+total_operation)*100:.0f}%")
        print("=" * 60)


class TestCombinedOverhead:
    """Combined workflow overhead analysis."""

    @pytest.mark.asyncio
    async def test_read_then_reply_overhead(self, gmail_imap_creds, gmail_smtp_creds):
        """Measure overhead for a read-then-reply workflow."""
        imap_handler = StatelessIMAPHandler(gmail_imap_creds)
        smtp_handler = StatelessSMTPHandler(gmail_smtp_creds)
        test_email = gmail_smtp_creds["user"]
        
        # Fetch
        imap_result = await imap_handler.fetch_messages_instrumented(folder="INBOX", limit=1)
        imap_timing = imap_result["timing"]
        
        # Reply
        smtp_result = await smtp_handler.send_message_instrumented(
            to=test_email,
            subject="[Workflow Test] Reply",
            body="Automated reply.",
        )
        smtp_timing = smtp_result["timing"]
        
        # Calculate totals
        imap_overhead = imap_timing["connect_ms"] + imap_timing["login_ms"] + imap_timing["logout_ms"]
        imap_operation = imap_timing["select_ms"] + imap_timing["search_ms"] + imap_timing["fetch_ms"]
        smtp_overhead = smtp_timing["connect_ms"] + smtp_timing["login_ms"] + smtp_timing["quit_ms"]
        smtp_operation = smtp_timing["send_ms"]
        
        total_overhead = imap_overhead + smtp_overhead
        total_operation = imap_operation + smtp_operation
        total_time = imap_timing["total_ms"] + smtp_timing["total_ms"]
        
        print("\n" + "=" * 60)
        print("READ-THEN-REPLY WORKFLOW BREAKDOWN")
        print("=" * 60)
        print(f"  IMAP overhead:      {imap_overhead:>8.0f} ms")
        print(f"  IMAP operation:     {imap_operation:>8.0f} ms")
        print(f"  SMTP overhead:      {smtp_overhead:>8.0f} ms")
        print(f"  SMTP operation:     {smtp_operation:>8.0f} ms")
        print("-" * 60)
        print(f"  TOTAL OVERHEAD:     {total_overhead:>8.0f} ms ({total_overhead/total_time*100:.0f}%)")
        print(f"  TOTAL OPERATION:    {total_operation:>8.0f} ms ({total_operation/total_time*100:.0f}%)")
        print(f"  TOTAL TIME:         {total_time:>8.0f} ms")
        print("=" * 60)
        print(f"\n  With connection pooling, overhead could be ~0ms!")
        print(f"  Potential speedup: {total_time/total_operation:.1f}x")
