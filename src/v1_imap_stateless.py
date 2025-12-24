"""Stateless IMAP handler - creates fresh connection for every operation."""

from typing import List
import aioimaplib
import email
from email.header import decode_header


class StatelessIMAPHandler:
    """
    Naive stateless IMAP handler that creates a new connection for every request.
    This is intentionally inefficient to demonstrate the need for connection pooling.
    """

    def __init__(self, credentials: dict):
        self.creds = credentials

    async def fetch_messages(self, folder: str, limit: int = 10) -> List[dict]:
        """
        Fetch messages from the specified folder.
        
        Creates a fresh connection for each call - intentionally slow.
        
        Args:
            folder: IMAP folder to fetch from (e.g., "INBOX")
            limit: Maximum number of messages to fetch
            
        Returns:
            List of parsed message dictionaries
        """
        result = await self.fetch_messages_instrumented(folder, limit)
        return result["messages"]

    async def fetch_messages_instrumented(
        self, folder: str, limit: int = 10
    ) -> dict:
        """
        Fetch messages with detailed timing for each phase.
        
        Returns:
            dict with 'messages' and 'timing' breakdown
        """
        import time
        timing = {}
        
        # 1. Connect fresh
        t0 = time.perf_counter()
        imap = aioimaplib.IMAP4_SSL(self.creds["host"])
        await imap.wait_hello_from_server()
        timing["connect_ms"] = (time.perf_counter() - t0) * 1000

        # 2. Login
        t0 = time.perf_counter()
        await imap.login(self.creds["user"], self.creds["password"])
        timing["login_ms"] = (time.perf_counter() - t0) * 1000

        # 3. Select folder
        t0 = time.perf_counter()
        await imap.select(folder)
        timing["select_ms"] = (time.perf_counter() - t0) * 1000

        # 4. Search
        t0 = time.perf_counter()
        _, data = await imap.search("ALL")
        timing["search_ms"] = (time.perf_counter() - t0) * 1000
        
        # Handle empty inbox
        if not data or not data[0]:
            t0 = time.perf_counter()
            await imap.logout()
            timing["logout_ms"] = (time.perf_counter() - t0) * 1000
            timing["fetch_ms"] = 0
            timing["total_ms"] = sum(timing.values())
            return {"messages": [], "timing": timing}
        
        # Parse message IDs (may be bytes or string)
        raw_ids = data[0]
        if isinstance(raw_ids, bytes):
            raw_ids = raw_ids.decode("utf-8")
        msg_ids = raw_ids.split()[-limit:]

        # 5. Fetch
        t0 = time.perf_counter()
        messages = []
        for msg_id in msg_ids:
            # Ensure msg_id is a string
            if isinstance(msg_id, bytes):
                msg_id = msg_id.decode("utf-8")
            
            _, msg_data = await imap.fetch(msg_id, "(RFC822)")
            parsed = self._parse_message(msg_data)
            if parsed:
                messages.append(parsed)
        timing["fetch_ms"] = (time.perf_counter() - t0) * 1000

        # 6. Disconnect (CRITICAL: always close)
        t0 = time.perf_counter()
        await imap.logout()
        timing["logout_ms"] = (time.perf_counter() - t0) * 1000
        
        timing["total_ms"] = sum(timing.values())
        
        return {"messages": messages, "timing": timing}

    def _parse_message(self, raw_data) -> dict:
        """Parse raw IMAP response into message dict."""
        try:
            # aioimaplib returns a list of response lines
            # Find the actual email content
            for item in raw_data:
                if isinstance(item, bytes):
                    msg = email.message_from_bytes(item)
                    return self._extract_message_info(msg)
                elif isinstance(item, tuple) and len(item) >= 2:
                    if isinstance(item[1], bytes):
                        msg = email.message_from_bytes(item[1])
                        return self._extract_message_info(msg)
            
            # Fallback: return raw data
            return {"raw": raw_data}
        except Exception as e:
            return {"raw": raw_data, "error": str(e)}

    def _extract_message_info(self, msg) -> dict:
        """Extract key info from email message."""
        subject = msg.get("Subject", "")
        if subject:
            decoded_parts = decode_header(subject)
            subject = ""
            for part, encoding in decoded_parts:
                if isinstance(part, bytes):
                    subject += part.decode(encoding or "utf-8", errors="replace")
                else:
                    subject += part
        
        return {
            "subject": subject,
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "date": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
        }
