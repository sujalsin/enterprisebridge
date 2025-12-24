"""In-memory IMAP connection pool - Phase 2 implementation.

This pool maintains persistent IMAP connections to avoid the repeated
connect/login overhead demonstrated in Phase 1.

LIMITATION: Connections are lost on application restart (in-memory only).
"""

import time
from typing import Dict, List, Optional

import aioimaplib
import email
from email.header import decode_header


class InMemoryIMAPPool:
    """
    Simple in-memory IMAP connection pool.
    
    Maintains a dictionary of authenticated IMAP connections keyed by user.
    Connections are reused across multiple requests, eliminating login overhead.
    
    LIMITATION: All connections are lost when the application restarts.
    """

    def __init__(self, max_connections: int = 5):
        self.max_connections = max_connections
        self.connections: Dict[str, aioimaplib.IMAP4_SSL] = {}
        self._connection_times: Dict[str, float] = {}

    async def get_connection(self, user: str, creds: dict) -> aioimaplib.IMAP4_SSL:
        """
        Get or create a connection for the given user.
        
        If a connection exists and is alive, reuse it.
        Otherwise, create a new authenticated connection.
        """
        if user in self.connections:
            # Reuse existing connection
            return self.connections[user]

        # Check pool size limit
        if len(self.connections) >= self.max_connections:
            # Evict oldest connection
            oldest_user = min(self._connection_times, key=self._connection_times.get)
            old_conn = self.connections.pop(oldest_user)
            self._connection_times.pop(oldest_user)
            try:
                await old_conn.logout()
            except Exception:
                pass

        # Create new connection
        imap = aioimaplib.IMAP4_SSL(creds["host"])
        await imap.wait_hello_from_server()
        await imap.login(creds["user"], creds["password"])

        self.connections[user] = imap
        self._connection_times[user] = time.time()

        return imap

    async def release_connection(self, user: str):
        """
        Release a connection back to the pool.
        
        For in-memory pool, this is a no-op since we keep connections alive.
        """
        # No-op - connection stays in pool
        pass

    async def close_all(self):
        """Close all connections in the pool."""
        for user, conn in list(self.connections.items()):
            try:
                await conn.logout()
            except Exception:
                pass
        self.connections.clear()
        self._connection_times.clear()

    def get_stats(self) -> dict:
        """Get pool statistics."""
        return {
            "active_connections": len(self.connections),
            "max_connections": self.max_connections,
            "users": list(self.connections.keys()),
        }


class PooledIMAPHandler:
    """
    IMAP handler that uses a connection pool.
    
    Unlike StatelessIMAPHandler, this reuses connections from a pool,
    eliminating the connect/login overhead for subsequent requests.
    """

    def __init__(self, pool: InMemoryIMAPPool, credentials: dict):
        self.pool = pool
        self.creds = credentials

    async def fetch_messages(self, folder: str, limit: int = 10) -> List[dict]:
        """
        Fetch messages using a pooled connection.
        
        The connection is NOT closed after use - it stays in the pool.
        """
        result = await self.fetch_messages_instrumented(folder, limit)
        return result["messages"]

    async def fetch_messages_instrumented(
        self, folder: str, limit: int = 10
    ) -> dict:
        """
        Fetch messages with timing breakdown.
        
        Returns dict with 'messages' and 'timing'.
        """
        timing = {}

        # Get connection from pool (may be cached)
        t0 = time.perf_counter()
        imap = await self.pool.get_connection(self.creds["user"], self.creds)
        timing["get_connection_ms"] = (time.perf_counter() - t0) * 1000

        # Select folder
        t0 = time.perf_counter()
        await imap.select(folder)
        timing["select_ms"] = (time.perf_counter() - t0) * 1000

        # Search
        t0 = time.perf_counter()
        _, data = await imap.search("ALL")
        timing["search_ms"] = (time.perf_counter() - t0) * 1000

        # Handle empty inbox
        if not data or not data[0]:
            timing["fetch_ms"] = 0
            timing["total_ms"] = sum(timing.values())
            return {"messages": [], "timing": timing}

        # Parse message IDs
        raw_ids = data[0]
        if isinstance(raw_ids, bytes):
            raw_ids = raw_ids.decode("utf-8")
        msg_ids = raw_ids.split()[-limit:]

        # Fetch messages
        t0 = time.perf_counter()
        messages = []
        for msg_id in msg_ids:
            if isinstance(msg_id, bytes):
                msg_id = msg_id.decode("utf-8")

            _, msg_data = await imap.fetch(msg_id, "(RFC822)")
            parsed = self._parse_message(msg_data)
            if parsed:
                messages.append(parsed)
        timing["fetch_ms"] = (time.perf_counter() - t0) * 1000

        # DON'T logout - connection stays in pool
        timing["total_ms"] = sum(timing.values())

        return {"messages": messages, "timing": timing}

    def _parse_message(self, raw_data) -> dict:
        """Parse raw IMAP response into message dict."""
        try:
            for item in raw_data:
                if isinstance(item, bytes):
                    msg = email.message_from_bytes(item)
                    return self._extract_message_info(msg)
                elif isinstance(item, tuple) and len(item) >= 2:
                    if isinstance(item[1], bytes):
                        msg = email.message_from_bytes(item[1])
                        return self._extract_message_info(msg)
            return {"raw": raw_data}
        except Exception as e:
            return {"raw": raw_data, "error": str(e)}

    def _extract_message_info(self, msg) -> dict:
        """Extract key info from email message."""
        subject = msg.get("Subject", "")
        if subject:
            decoded_parts = decode_header(subject)
            subject = ""
            for part, enc in decoded_parts:
                if isinstance(part, bytes):
                    subject += part.decode(enc or "utf-8", errors="replace")
                else:
                    subject += part

        return {
            "subject": subject,
            "from": msg.get("From", ""),
            "to": msg.get("To", ""),
            "date": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
        }
