"""In-memory SMTP connection pool - Phase 2 implementation.

This pool maintains persistent SMTP connections to avoid the repeated
connect/login overhead demonstrated in Phase 1.

LIMITATION: Connections are lost on application restart (in-memory only).
"""

import time
from email.mime.text import MIMEText
from typing import Dict, Optional

import aiosmtplib


class InMemorySMTPPool:
    """
    Simple in-memory SMTP connection pool.
    
    Maintains a dictionary of authenticated SMTP connections keyed by user.
    Connections are reused across multiple sends, eliminating login overhead.
    
    LIMITATION: All connections are lost when the application restarts.
    """

    def __init__(self, max_connections: int = 5):
        self.max_connections = max_connections
        self.connections: Dict[str, aiosmtplib.SMTP] = {}
        self._connection_times: Dict[str, float] = {}

    async def get_connection(self, user: str, creds: dict) -> aiosmtplib.SMTP:
        """
        Get or create a connection for the given user.
        
        If a connection exists and is alive, reuse it.
        Otherwise, create a new authenticated connection.
        """
        if user in self.connections:
            # Check if connection is still alive
            smtp = self.connections[user]
            if smtp.is_connected:
                return smtp
            else:
                # Connection died, remove it
                del self.connections[user]
                del self._connection_times[user]

        # Check pool size limit
        if len(self.connections) >= self.max_connections:
            # Evict oldest connection
            oldest_user = min(self._connection_times, key=self._connection_times.get)
            old_conn = self.connections.pop(oldest_user)
            self._connection_times.pop(oldest_user)
            try:
                await old_conn.quit()
            except Exception:
                pass

        # Create new connection
        smtp = aiosmtplib.SMTP(
            hostname=creds["host"],
            port=creds["port"],
            use_tls=creds.get("use_tls", False),
        )
        await smtp.connect()
        await smtp.login(creds["user"], creds["password"])

        self.connections[user] = smtp
        self._connection_times[user] = time.time()

        return smtp

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
                await conn.quit()
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


class PooledSMTPHandler:
    """
    SMTP handler that uses a connection pool.
    
    Unlike StatelessSMTPHandler, this reuses connections from a pool,
    eliminating the connect/login overhead for subsequent sends.
    """

    def __init__(self, pool: InMemorySMTPPool, credentials: dict):
        self.pool = pool
        self.creds = credentials

    async def send_message(
        self, to: str, subject: str, body: str = "", html_body: Optional[str] = None
    ) -> dict:
        """
        Send a message using a pooled connection.
        
        The connection is NOT closed after use - it stays in the pool.
        """
        result = await self.send_message_instrumented(to, subject, body, html_body)
        return {"status": result["status"], "message_id": result["message_id"]}

    async def send_message_instrumented(
        self, to: str, subject: str, body: str = "", html_body: Optional[str] = None
    ) -> dict:
        """
        Send message with timing breakdown.
        
        Returns dict with 'status', 'message_id', and 'timing'.
        """
        timing = {}

        # Get connection from pool (may be cached)
        t0 = time.perf_counter()
        smtp = await self.pool.get_connection(self.creds["user"], self.creds)
        timing["get_connection_ms"] = (time.perf_counter() - t0) * 1000

        # Build message
        message = MIMEText(body)
        message["From"] = self.creds["user"]
        message["To"] = to
        message["Subject"] = subject

        # Send
        t0 = time.perf_counter()
        await smtp.send_message(message)
        timing["send_ms"] = (time.perf_counter() - t0) * 1000

        # DON'T quit - connection stays in pool
        timing["total_ms"] = sum(timing.values())

        return {
            "status": "sent",
            "message_id": message.get("Message-ID"),
            "timing": timing,
        }
