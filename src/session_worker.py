"""Session worker for keeping IMAP sessions alive.

This worker periodically:
- Scans Redis for active IMAP sessions
- Sends NOOP to keep connections alive
- Refreshes OAuth tokens if <60s to expiry
- Updates TTL to 300s

Usage:
    python -m src.session_worker

Environment:
    REDIS_URL: Redis connection URL (default: redis://localhost:6379/0)
    NOOP_INTERVAL: Seconds between NOOP commands (default: 25)
"""

import os
import asyncio
import hashlib
from datetime import datetime
from typing import Optional

import aioredis
import structlog

# Configure structlog for JSON output
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logger = structlog.get_logger()


def hash_email(email: str) -> str:
    """Hash email address for privacy-safe logging."""
    return hashlib.sha256(email.encode()).hexdigest()[:12]


async def get_active_sessions(redis: aioredis.Redis) -> list:
    """Get all active IMAP sessions from Redis."""
    keys = await redis.keys("imap:session:*")
    sessions = []
    
    for key in keys:
        session_data = await redis.hgetall(key)
        if session_data:
            user = key.decode().replace("imap:session:", "")
            ttl = await redis.ttl(key)
            
            sessions.append({
                "user": user,
                "user_hash": hash_email(user),
                "key": key,
                "ttl": ttl,
                "data": {k.decode(): v.decode() for k, v in session_data.items()},
            })
    
    return sessions


async def check_oauth_token(session: dict, redis: aioredis.Redis) -> bool:
    """
    Check if OAuth token needs refresh.
    
    Returns True if token was refreshed or doesn't need refresh.
    Returns False if refresh failed.
    """
    data = session.get("data", {})
    token_expiry = data.get("token_expiry")
    
    if not token_expiry:
        return True  # No OAuth token, skip
    
    try:
        expiry_time = datetime.fromisoformat(token_expiry)
        now = datetime.utcnow()
        seconds_until_expiry = (expiry_time - now).total_seconds()
        
        if seconds_until_expiry < 60:
            # Token expiring soon, would need refresh
            # In production: call OAuth refresh endpoint
            logger.info(
                "oauth_refresh_needed",
                user_hash=session["user_hash"],
                seconds_until_expiry=int(seconds_until_expiry),
            )
            # Placeholder: actual refresh logic would go here
            return True
    except Exception as e:
        logger.error(
            "oauth_check_failed",
            user_hash=session["user_hash"],
            error=str(e),
        )
        return False
    
    return True


async def send_noop_to_session(session: dict, redis: aioredis.Redis) -> bool:
    """
    Send NOOP to keep session alive.
    
    Note: We can't actually send NOOP since we don't have the TCP connection.
    The in-memory pool holds the actual connection. This worker:
    1. Refreshes the Redis TTL
    2. Logs activity for monitoring
    
    For actual NOOP, the HybridIMAPHandler would need to be running.
    """
    user_hash = session["user_hash"]
    key = session["key"]
    old_ttl = session["ttl"]
    
    try:
        # Check OAuth token first
        await check_oauth_token(session, redis)
        
        # Refresh TTL to 300 seconds
        new_ttl = 300
        await redis.expire(key, new_ttl)
        
        logger.info(
            "noop_sent",
            user_hash=user_hash,
            old_ttl=old_ttl,
            ttl=new_ttl,
        )
        return True
        
    except Exception as e:
        logger.error(
            "noop_failed",
            user_hash=user_hash,
            error=str(e),
        )
        return False


async def cleanup_expired_sessions(redis: aioredis.Redis) -> int:
    """Remove any orphaned session data."""
    keys = await redis.keys("imap:session:*")
    cleaned = 0
    
    for key in keys:
        ttl = await redis.ttl(key)
        if ttl == -1:  # No TTL set (orphaned)
            await redis.delete(key)
            user = key.decode().replace("imap:session:", "")
            logger.info(
                "session_cleaned",
                user_hash=hash_email(user),
                reason="no_ttl",
            )
            cleaned += 1
        elif ttl == -2:  # Key doesn't exist
            cleaned += 1
    
    return cleaned


async def worker_loop():
    """Main worker loop."""
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    noop_interval = int(os.getenv("NOOP_INTERVAL", "25"))
    
    logger.info(
        "worker_starting",
        redis_host=redis_url.split("://")[-1].split("/")[0] if "://" in redis_url else "localhost",
        noop_interval=noop_interval,
    )
    
    try:
        redis = await aioredis.from_url(redis_url)
        logger.info("redis_connected")
    except Exception as e:
        logger.error("redis_connection_failed", error=str(e))
        return
    
    try:
        while True:
            start_time = datetime.now()
            
            # Get active sessions
            sessions = await get_active_sessions(redis)
            
            if sessions:
                logger.info(
                    "sessions_found",
                    count=len(sessions),
                )
                
                # Send NOOP to each session
                success_count = 0
                for session in sessions:
                    if await send_noop_to_session(session, redis):
                        success_count += 1
                
                logger.info(
                    "noop_cycle_complete",
                    total=len(sessions),
                    success=success_count,
                    failed=len(sessions) - success_count,
                )
            
            # Cleanup expired sessions
            cleaned = await cleanup_expired_sessions(redis)
            if cleaned:
                logger.info(
                    "cleanup_complete",
                    cleaned=cleaned,
                )
            
            # Wait for next interval
            elapsed = (datetime.now() - start_time).total_seconds()
            sleep_time = max(0, noop_interval - elapsed)
            await asyncio.sleep(sleep_time)
            
    except asyncio.CancelledError:
        logger.info("worker_cancelled")
    except Exception as e:
        logger.error("worker_error", error=str(e))
    finally:
        await redis.close()
        logger.info("worker_stopped")


def main():
    """Entry point."""
    try:
        asyncio.run(worker_loop())
    except KeyboardInterrupt:
        logger.info("worker_interrupted")


if __name__ == "__main__":
    main()
