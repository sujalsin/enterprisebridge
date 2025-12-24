"""Redis-backed SMTP connection pool.

Phase 3: Redis session persistence for SMTP connections.

Features:
- Session metadata persisted in Redis
- Survives application restarts
- TTL-based session expiry
- In-memory connection reuse

Usage:
    pool = RedisSMTPPool("redis://localhost:6379/0")
    handler = HybridSMTPHandler(pool, credentials)
    result = await handler.send_message(to="...", subject="...", body="...")
"""

import os
import asyncio
import time
from typing import Optional, Dict, Any

import aioredis
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


class RedisSMTPPool:
    """
    Redis-backed SMTP connection pool.
    
    Stores session metadata in Redis, not actual TCP connections.
    The in-memory connection is maintained by the handler.
    """
    
    def __init__(self, redis_url: str = "redis://localhost:6379/0", max_connections: int = 10):
        self.redis_url = redis_url
        self.max_connections = max_connections
        self.redis: Optional[aioredis.Redis] = None
        self.stats = {"hits": 0, "misses": 0, "reused": 0, "created": 0}
    
    async def _ensure_redis(self):
        """Ensure Redis connection is established."""
        if self.redis is None:
            self.redis = await aioredis.from_url(self.redis_url)
    
    async def store_session(self, user: str, metadata: Dict[str, Any], ttl: int = 300):
        """Store session metadata in Redis."""
        await self._ensure_redis()
        key = f"smtp:session:{user}"
        await self.redis.hset(key, mapping={
            "host": metadata.get("host", ""),
            "port": str(metadata.get("port", 587)),
            "last_used": str(time.time()),
            "created_at": str(metadata.get("created_at", time.time())),
        })
        await self.redis.expire(key, ttl)
    
    async def get_session(self, user: str) -> Optional[Dict[str, Any]]:
        """Get session metadata from Redis."""
        await self._ensure_redis()
        key = f"smtp:session:{user}"
        data = await self.redis.hgetall(key)
        
        if data:
            self.stats["hits"] += 1
            return {
                k.decode() if isinstance(k, bytes) else k: 
                v.decode() if isinstance(v, bytes) else v 
                for k, v in data.items()
            }
        
        self.stats["misses"] += 1
        return None
    
    async def refresh_ttl(self, user: str, ttl: int = 300):
        """Refresh session TTL."""
        await self._ensure_redis()
        key = f"smtp:session:{user}"
        await self.redis.expire(key, ttl)
        # Update last_used
        await self.redis.hset(key, "last_used", str(time.time()))
    
    async def get_ttl(self, user: str) -> int:
        """Get remaining TTL for a session."""
        await self._ensure_redis()
        key = f"smtp:session:{user}"
        return await self.redis.ttl(key)
    
    async def delete_session(self, user: str):
        """Delete session from Redis."""
        await self._ensure_redis()
        key = f"smtp:session:{user}"
        await self.redis.delete(key)
    
    async def close(self):
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            self.redis = None


class HybridSMTPHandler:
    """
    Hybrid SMTP handler with Redis session awareness.
    
    - Checks Redis for existing session metadata
    - Maintains in-memory SMTP connection
    - Stores session metadata in Redis for persistence
    """
    
    def __init__(self, pool: RedisSMTPPool, credentials: Dict[str, Any]):
        self.pool = pool
        self.credentials = credentials
        self.smtp: Optional[aiosmtplib.SMTP] = None
        self._connected = False
    
    async def _get_connection(self) -> aiosmtplib.SMTP:
        """Get or create SMTP connection."""
        user = self.credentials["user"]
        
        # Check Redis for existing session
        session = await self.pool.get_session(user)
        
        if session and self.smtp and self._connected:
            # Reuse existing connection
            self.pool.stats["reused"] += 1
            await self.pool.refresh_ttl(user)
            return self.smtp
        
        # Create new connection
        host = self.credentials.get("host", "smtp.gmail.com")
        port = self.credentials.get("port", 587)
        use_tls = self.credentials.get("use_tls", False)
        start_tls = self.credentials.get("start_tls", True)
        
        self.smtp = aiosmtplib.SMTP(
            hostname=host,
            port=port,
            use_tls=use_tls,
            start_tls=start_tls,
        )
        
        await self.smtp.connect()
        
        # Login
        await self.smtp.login(
            self.credentials["user"],
            self.credentials["password"],
        )
        
        self._connected = True
        self.pool.stats["created"] += 1
        
        # Store session in Redis
        await self.pool.store_session(user, {
            "host": host,
            "port": port,
            "created_at": time.time(),
        })
        
        return self.smtp
    
    async def send_message(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an email message."""
        smtp = await self._get_connection()
        
        # Build message
        if html_body:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain"))
            msg.attach(MIMEText(html_body, "html"))
        else:
            msg = MIMEText(body)
        
        msg["Subject"] = subject
        msg["From"] = self.credentials["user"]
        msg["To"] = to
        
        # Send
        await smtp.send_message(msg)
        
        # Refresh TTL
        await self.pool.refresh_ttl(self.credentials["user"])
        
        return {"status": "sent", "to": to, "subject": subject}
    
    async def send_message_instrumented(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send message with timing instrumentation."""
        timing = {}
        total_start = time.perf_counter()
        
        # Get connection
        start = time.perf_counter()
        smtp = await self._get_connection()
        timing["get_connection_ms"] = (time.perf_counter() - start) * 1000
        
        # Build message
        if html_body:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "plain"))
            msg.attach(MIMEText(html_body, "html"))
        else:
            msg = MIMEText(body)
        
        msg["Subject"] = subject
        msg["From"] = self.credentials["user"]
        msg["To"] = to
        
        # Send
        start = time.perf_counter()
        await smtp.send_message(msg)
        timing["send_ms"] = (time.perf_counter() - start) * 1000
        
        # Refresh TTL
        start = time.perf_counter()
        await self.pool.refresh_ttl(self.credentials["user"])
        timing["refresh_ms"] = (time.perf_counter() - start) * 1000
        
        timing["total_ms"] = (time.perf_counter() - total_start) * 1000
        
        return {
            "status": "sent",
            "to": to,
            "subject": subject,
            "timing": timing,
        }
    
    async def close(self):
        """Close SMTP connection."""
        if self.smtp:
            try:
                await self.smtp.quit()
            except Exception:
                pass
            self.smtp = None
            self._connected = False
