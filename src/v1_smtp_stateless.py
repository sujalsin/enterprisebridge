"""Stateless SMTP handler - creates fresh connection for every send operation."""

import time
from email.mime.text import MIMEText
from typing import Optional

import aiosmtplib


class StatelessSMTPHandler:
    """
    Naive stateless SMTP handler that creates a new connection for every send.
    This is intentionally inefficient to demonstrate the need for connection pooling.
    """

    def __init__(self, credentials: dict):
        self.creds = credentials

    async def send_message(
        self, to: str, subject: str, body: str = "", html_body: Optional[str] = None
    ) -> dict:
        """
        Send an email message.
        
        Creates a fresh connection for each send - intentionally slow.
        
        Args:
            to: Recipient email address
            subject: Email subject
            body: Plain text body
            html_body: Optional HTML body
            
        Returns:
            Dict with status and message_id
        """
        result = await self.send_message_instrumented(to, subject, body, html_body)
        return {"status": result["status"], "message_id": result["message_id"]}

    async def send_message_instrumented(
        self, to: str, subject: str, body: str = "", html_body: Optional[str] = None
    ) -> dict:
        """
        Send message with detailed timing for each phase.
        
        Returns:
            dict with 'status', 'message_id', and 'timing' breakdown
        """
        timing = {}
        
        # 1. Create SMTP client (no network yet)
        smtp = aiosmtplib.SMTP(
            hostname=self.creds["host"],
            port=self.creds["port"],
            use_tls=self.creds.get("use_tls", True),
        )

        # 2. Connect (TCP + TLS handshake)
        t0 = time.perf_counter()
        await smtp.connect()
        timing["connect_ms"] = (time.perf_counter() - t0) * 1000
        
        # 3. Login
        t0 = time.perf_counter()
        await smtp.login(self.creds["user"], self.creds["password"])
        timing["login_ms"] = (time.perf_counter() - t0) * 1000

        # 4. Build message
        message = MIMEText(body)
        message["From"] = self.creds["user"]
        message["To"] = to
        message["Subject"] = subject

        # 5. Send
        t0 = time.perf_counter()
        await smtp.send_message(message)
        timing["send_ms"] = (time.perf_counter() - t0) * 1000

        # 6. Disconnect
        t0 = time.perf_counter()
        await smtp.quit()
        timing["quit_ms"] = (time.perf_counter() - t0) * 1000
        
        timing["total_ms"] = sum(timing.values())

        return {
            "status": "sent",
            "message_id": message.get("Message-ID"),
            "timing": timing,
        }
