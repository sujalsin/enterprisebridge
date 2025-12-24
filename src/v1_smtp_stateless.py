"""Stateless SMTP handler - creates fresh connection for every send operation."""

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
        smtp = aiosmtplib.SMTP(
            hostname=self.creds["host"],
            port=self.creds["port"],
            use_tls=self.creds.get("use_tls", True),
        )

        # Connect + Auth (slow)
        await smtp.connect()
        await smtp.login(self.creds["user"], self.creds["password"])

        # Build message
        message = MIMEText(body)
        message["From"] = self.creds["user"]
        message["To"] = to
        message["Subject"] = subject

        # Send
        await smtp.send_message(message)

        # Disconnect
        await smtp.quit()

        return {"status": "sent", "message_id": message.get("Message-ID")}
