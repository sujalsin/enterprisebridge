"""Proxy API that mimics AgentMail's official SDK interface.

This module provides a FastAPI-based proxy that:
1. Matches AgentMail's SDK method signatures
2. Translates calls to legacy IMAP/SMTP
3. Returns responses in AgentMail's Pydantic schema format

Usage:
    uvicorn src.v3_proxy_api:app --host 0.0.0.0 --port 8000
"""

import os
from typing import List, Optional
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field, ConfigDict

from src.v3_imap_redis_pool import RedisIMAPPool, HybridIMAPHandler
from src.v2_smtp_memory_pool import InMemorySMTPPool, PooledSMTPHandler
from src.v3_transformer_rag import transform_to_rag


# ============================================================
# Pydantic Models (matching AgentMail SDK schema)
# ============================================================

class Attachment(BaseModel):
    """Attachment model matching AgentMail's schema."""
    filename: str
    content_type: Optional[str] = None
    size: int = 0
    extracted_text: Optional[str] = None


class Message(BaseModel):
    """Message model matching AgentMail's schema."""
    id: Optional[str] = Field(None, alias="message_id")
    thread_id: Optional[str] = None
    subject: str = ""
    from_: str = Field("", alias="from")
    to: str = ""
    date: Optional[str] = None
    body: str = ""
    attachments: List[Attachment] = []
    
    model_config = ConfigDict(populate_by_name=True)


class MessageListResponse(BaseModel):
    """Response for listing messages."""
    data: List[Message]
    source: str = "legacy"
    count: int = 0


class MessageSendRequest(BaseModel):
    """Request to send a message."""
    to: str
    subject: str
    body: str
    html_body: Optional[str] = None


class MessageSendResponse(BaseModel):
    """Response after sending a message."""
    status: str
    message_id: Optional[str] = None


class InboxCreateRequest(BaseModel):
    """Request to create an inbox mapping."""
    email: str
    imap_host: str = "imap.gmail.com"
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    username: str
    password: str


class InboxCreateResponse(BaseModel):
    """Response after creating an inbox."""
    inbox_id: str
    email: str
    status: str = "active"


# ============================================================
# Application Setup
# ============================================================

from contextlib import asynccontextmanager

# Global pools (initialized on startup)
redis_pool: Optional[RedisIMAPPool] = None
smtp_pool: Optional[InMemorySMTPPool] = None

# In-memory credential store (in production, use a vault)
credential_store: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global redis_pool, smtp_pool
    
    # Startup
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_pool = RedisIMAPPool(redis_url)
    smtp_pool = InMemorySMTPPool(max_connections=10)
    
    yield
    
    # Shutdown
    if redis_pool:
        await redis_pool.close()
    if smtp_pool:
        await smtp_pool.close_all()


app = FastAPI(
    title="AgentMail Proxy",
    description="Proxy API that bridges legacy IMAP/SMTP to AgentMail's SDK interface",
    version="0.3.0",
    lifespan=lifespan,
)


def get_credentials(inbox_id: str) -> dict:
    """Get credentials for an inbox."""
    if inbox_id in credential_store:
        return credential_store[inbox_id]
    
    # Try environment variables as fallback
    email = os.getenv("TEST_GMAIL_EMAIL")
    password = os.getenv("TEST_GMAIL_PASSWORD")
    
    if email and password:
        return {
            "host": "imap.gmail.com",
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "user": email,
            "password": password,
        }
    
    raise HTTPException(status_code=404, detail=f"Inbox {inbox_id} not found")


# ============================================================
# API Endpoints (matching AgentMail SDK)
# ============================================================

@app.get("/v1/inboxes/{inbox_id}/messages", response_model=MessageListResponse)
async def list_messages(
    inbox_id: str,
    folder: str = "INBOX",
    limit: int = 10,
):
    """
    List messages from a legacy inbox.
    
    Matches: client.messages.list(inbox_id=...)
    """
    creds = get_credentials(inbox_id)
    
    # Use hybrid handler with Redis session awareness
    handler = HybridIMAPHandler(redis_pool, creds)
    
    try:
        result = await handler.fetch_messages_instrumented(folder=folder, limit=limit)
        raw_messages = result["messages"]
        
        # Transform each message to RAG format and then to Message model
        messages = []
        for raw in raw_messages:
            msg = Message(
                message_id=raw.get("message_id", ""),
                thread_id=raw.get("thread_id", ""),
                subject=raw.get("subject", ""),
                from_=raw.get("from", ""),
                to=raw.get("to", ""),
                date=raw.get("date", ""),
                body=raw.get("body", ""),
                attachments=[],
            )
            messages.append(msg)
        
        return MessageListResponse(
            data=messages,
            source="legacy",
            count=len(messages),
        )
    finally:
        # Note: We don't close the handler - connection stays in pool
        pass


@app.post("/v1/inboxes/{inbox_id}/messages", response_model=MessageSendResponse)
async def send_message(inbox_id: str, request: MessageSendRequest):
    """
    Send a message from a legacy inbox.
    
    Matches: client.messages.send(inbox_id=..., to=..., subject=..., body=...)
    """
    creds = get_credentials(inbox_id)
    
    smtp_creds = {
        "host": creds.get("smtp_host", "smtp.gmail.com"),
        "port": creds.get("smtp_port", 587),
        "user": creds["user"],
        "password": creds["password"],
        "use_tls": False,
    }
    
    handler = PooledSMTPHandler(smtp_pool, smtp_creds)
    
    result = await handler.send_message(
        to=request.to,
        subject=request.subject,
        body=request.body,
        html_body=request.html_body,
    )
    
    return MessageSendResponse(
        status=result["status"],
        message_id=result.get("message_id"),
    )


@app.post("/v1/inboxes", response_model=InboxCreateResponse)
async def create_inbox(request: InboxCreateRequest):
    """
    Create/register a legacy inbox mapping.
    
    Matches: client.inboxes.create(email=..., ...)
    """
    inbox_id = request.email
    
    # Store credentials
    credential_store[inbox_id] = {
        "host": request.imap_host,
        "smtp_host": request.smtp_host,
        "smtp_port": request.smtp_port,
        "user": request.username,
        "password": request.password,
    }
    
    return InboxCreateResponse(
        inbox_id=inbox_id,
        email=request.email,
        status="active",
    )


@app.get("/v1/inboxes/{inbox_id}")
async def get_inbox(inbox_id: str):
    """Get inbox details."""
    if inbox_id not in credential_store:
        # Check if env fallback is available
        if os.getenv("TEST_GMAIL_EMAIL"):
            return {
                "inbox_id": inbox_id,
                "email": os.getenv("TEST_GMAIL_EMAIL"),
                "status": "active",
                "source": "environment",
            }
        raise HTTPException(status_code=404, detail=f"Inbox {inbox_id} not found")
    
    creds = credential_store[inbox_id]
    return {
        "inbox_id": inbox_id,
        "email": creds["user"],
        "status": "active",
    }


@app.delete("/v1/inboxes/{inbox_id}")
async def delete_inbox(inbox_id: str):
    """Delete an inbox mapping."""
    if inbox_id in credential_store:
        del credential_store[inbox_id]
        return {"status": "deleted", "inbox_id": inbox_id}
    raise HTTPException(status_code=404, detail=f"Inbox {inbox_id} not found")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "redis_connected": redis_pool is not None,
        "smtp_pool_size": len(smtp_pool.connections) if smtp_pool else 0,
    }


# ============================================================
# SDK Client Wrapper (for mimicking agentmail.Client)
# ============================================================

class MessagesResource:
    """Messages resource matching AgentMail SDK."""
    
    def __init__(self, client: "ProxyClient"):
        self._client = client
    
    async def list(self, inbox_id: str, folder: str = "INBOX", limit: int = 10) -> List[Message]:
        """List messages from an inbox."""
        import httpx
        async with httpx.AsyncClient() as http:
            response = await http.get(
                f"{self._client.base_url}/v1/inboxes/{inbox_id}/messages",
                params={"folder": folder, "limit": limit},
                headers={"Authorization": f"Bearer {self._client.api_key}"},
            )
            response.raise_for_status()
            data = response.json()
            return [Message(**msg) for msg in data["data"]]
    
    async def send(self, inbox_id: str, to: str, subject: str, body: str) -> MessageSendResponse:
        """Send a message."""
        import httpx
        async with httpx.AsyncClient() as http:
            response = await http.post(
                f"{self._client.base_url}/v1/inboxes/{inbox_id}/messages",
                json={"to": to, "subject": subject, "body": body},
                headers={"Authorization": f"Bearer {self._client.api_key}"},
            )
            response.raise_for_status()
            return MessageSendResponse(**response.json())


class InboxesResource:
    """Inboxes resource matching AgentMail SDK."""
    
    def __init__(self, client: "ProxyClient"):
        self._client = client
    
    async def create(self, email: str, username: str, password: str, **kwargs) -> InboxCreateResponse:
        """Create an inbox mapping."""
        import httpx
        async with httpx.AsyncClient() as http:
            response = await http.post(
                f"{self._client.base_url}/v1/inboxes",
                json={"email": email, "username": username, "password": password, **kwargs},
                headers={"Authorization": f"Bearer {self._client.api_key}"},
            )
            response.raise_for_status()
            return InboxCreateResponse(**response.json())
    
    async def get(self, inbox_id: str) -> dict:
        """Get inbox details."""
        import httpx
        async with httpx.AsyncClient() as http:
            response = await http.get(
                f"{self._client.base_url}/v1/inboxes/{inbox_id}",
                headers={"Authorization": f"Bearer {self._client.api_key}"},
            )
            response.raise_for_status()
            return response.json()


class ProxyClient:
    """
    Client that mimics AgentMail's official SDK interface.
    
    Usage:
        # Official AgentMail
        from agentmail import Client
        client = Client(api_key="...", base_url="https://api.agentmail.to")
        
        # Our Proxy (drop-in replacement)
        from src.v3_proxy_api import ProxyClient
        client = ProxyClient(api_key="...", base_url="http://localhost:8000")
        
        # Same method calls work!
        messages = await client.messages.list(inbox_id="user@example.com")
    """
    
    def __init__(self, api_key: str = "", base_url: str = "http://localhost:8000"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.messages = MessagesResource(self)
        self.inboxes = InboxesResource(self)
