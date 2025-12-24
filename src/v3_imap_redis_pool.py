"""Redis-backed IMAP connection pool - Phase 3 implementation.

This pool stores session metadata in Redis, allowing sessions to survive
application restarts. Active connections are still in-memory, but Redis
stores the session state needed to quickly re-establish connections.

Features:
- Session persistence across app restarts
- TTL-based session expiry with refresh
- Concurrent request handling
- Connection reuse within a process
"""

import json
import time
import asyncio
from typing import Dict, List, Optional

import aioredis
import aioimaplib
import email
from email.header import decode_header


class RedisIMAPPool:
    """
    Redis-backed IMAP session pool.
    
    Stores session metadata (not actual TCP connections) in Redis.
    This allows new application instances to "warm start" by knowing
    which users have active sessions.
    
    Note: TCP/IMAP connections cannot be serialized to Redis. What we store
    is session metadata that helps us quickly re-establish connections.
    """

    def __init__(self, redis_url: str, max_connections: int = 10, default_ttl: int = 300):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.max_connections = max_connections
        self.default_ttl = default_ttl
        self.stats = {
            "hits": 0,
            "misses": 0,
            "reused": 0,
            "created": 0,
        }

    async def store_session(self, user: str, session_data: dict, ttl: Optional[int] = None):
        """
        Store session metadata in Redis.
        
        Args:
            user: User identifier (email)
            session_data: Session metadata (host, auth info, etc.)
            ttl: Time-to-live in seconds (default: 300)
        """
        ttl = ttl or self.default_ttl
        key = f"imap:session:{user}"
        session_data["stored_at"] = time.time()
        await self.redis.setex(key, ttl, json.dumps(session_data))

    async def get_session(self, user: str) -> Optional[dict]:
        """
        Retrieve session metadata from Redis.
        
        Returns:
            Session data dict or None if not found/expired
        """
        key = f"imap:session:{user}"
        data = await self.redis.get(key)
        if data:
            self.stats["hits"] += 1
            return json.loads(data)
        self.stats["misses"] += 1
        return None

    async def refresh_ttl(self, user: str, ttl: Optional[int] = None):
        """
        Refresh the TTL of a session (keep-alive).
        
        Args:
            user: User identifier
            ttl: New TTL in seconds (default: use default_ttl)
        """
        ttl = ttl or self.default_ttl
        key = f"imap:session:{user}"
        await self.redis.expire(key, ttl)

    async def delete_session(self, user: str):
        """Remove a session from Redis."""
        key = f"imap:session:{user}"
        await self.redis.delete(key)

    async def get_ttl(self, user: str) -> int:
        """Get remaining TTL for a session."""
        key = f"imap:session:{user}"
        return await self.redis.ttl(key)

    async def list_sessions(self) -> List[str]:
        """List all active session keys."""
        keys = await self.redis.keys("imap:session:*")
        return [k.replace("imap:session:", "") for k in keys]

    async def close(self):
        """Close Redis connection."""
        await self.redis.close()

    def get_stats(self) -> dict:
        """Get pool statistics."""
        return {
            **self.stats,
            "hit_rate": self.stats["hits"] / max(1, self.stats["hits"] + self.stats["misses"]),
        }


class HybridIMAPHandler:
    """
    Hybrid IMAP handler with Redis-backed session awareness.
    
    Combines:
    - In-memory connection pool (fast, same-process reuse)
    - Redis session storage (persists across restarts)
    
    On restart, the handler can check Redis for existing sessions
    and warm up connections quickly.
    """

    def __init__(self, pool: RedisIMAPPool, credentials: dict):
        self.pool = pool
        self.creds = credentials
        self._active_connections: Dict[str, aioimaplib.IMAP4_SSL] = {}
        self._connection_times: Dict[str, float] = {}

    async def fetch_messages(self, folder: str, limit: int = 10) -> List[dict]:
        """Fetch messages using hybrid pooled connection."""
        result = await self.fetch_messages_instrumented(folder, limit)
        return result["messages"]

    async def fetch_messages_instrumented(
        self, folder: str, limit: int = 10
    ) -> dict:
        """Fetch messages with timing breakdown."""
        timing = {}
        user = self.creds["user"]

        # Check Redis for existing session
        t0 = time.perf_counter()
        session = await self.pool.get_session(user)
        timing["redis_check_ms"] = (time.perf_counter() - t0) * 1000

        # Try to reuse active in-memory connection
        t0 = time.perf_counter()
        if user in self._active_connections:
            imap = self._active_connections[user]
            self.pool.stats["reused"] += 1
            timing["get_connection_ms"] = (time.perf_counter() - t0) * 1000
        else:
            # Need to create new connection
            imap = await self._create_new_connection(user)
            self._active_connections[user] = imap
            self._connection_times[user] = time.time()
            self.pool.stats["created"] += 1
            timing["get_connection_ms"] = (time.perf_counter() - t0) * 1000

            # Store session in Redis for future restarts
            t0 = time.perf_counter()
            session_data = {
                "host": self.creds["host"],
                "user": user,
                "selected_folder": folder,
            }
            await self.pool.store_session(user, session_data)
            timing["redis_store_ms"] = (time.perf_counter() - t0) * 1000

        # Refresh TTL (keep session alive)
        await self.pool.refresh_ttl(user)

        # Select folder
        t0 = time.perf_counter()
        await imap.select(folder)
        timing["select_ms"] = (time.perf_counter() - t0) * 1000

        # Search
        t0 = time.perf_counter()
        _, data = await imap.search("ALL")
        timing["search_ms"] = (time.perf_counter() - t0) * 1000

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

        timing["total_ms"] = sum(timing.values())
        return {"messages": messages, "timing": timing}

    async def _create_new_connection(self, user: str) -> aioimaplib.IMAP4_SSL:
        """Create a new authenticated IMAP connection."""
        imap = aioimaplib.IMAP4_SSL(self.creds["host"])
        await imap.wait_hello_from_server()
        await imap.login(self.creds["user"], self.creds["password"])
        return imap

    async def close_all(self):
        """Close all active connections."""
        for user, conn in list(self._active_connections.items()):
            try:
                await conn.logout()
            except Exception:
                pass
        self._active_connections.clear()
        self._connection_times.clear()

    def _parse_message(self, raw_data) -> dict:
        """Parse raw IMAP response."""
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
