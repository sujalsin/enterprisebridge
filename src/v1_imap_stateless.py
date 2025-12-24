"""Stateless IMAP handler - creates fresh connection for every operation."""

from typing import List
import aioimaplib


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
        # 1. Connect fresh
        imap = aioimaplib.IMAP4_SSL(self.creds["host"])
        await imap.wait_hello_from_server()

        # 2. Login
        await imap.login(self.creds["user"], self.creds["password"])

        # 3. Select folder
        await imap.select(folder)

        # 4. Search
        _, data = await imap.search("ALL")
        msg_ids = data[0].split()[-limit:]

        # 5. Fetch
        messages = []
        for msg_id in msg_ids:
            _, msg_data = await imap.fetch(msg_id, "(RFC822)")
            messages.append(self._parse_message(msg_data))

        # 6. Disconnect (CRITICAL: always close)
        await imap.logout()

        return messages

    def _parse_message(self, raw_data) -> dict:
        """Simple parser - returns raw data wrapped in dict."""
        return {"raw": raw_data}
