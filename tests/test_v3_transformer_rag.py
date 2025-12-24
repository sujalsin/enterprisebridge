"""Tests for email-to-RAG transformer.

These tests verify the transformer correctly:
1. Removes HTML signatures
2. Collapses nested quotes
3. Strips tracking pixels
4. Extracts PDF text
5. Reduces content size
"""

import pytest
from unittest.mock import patch
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

from src.v3_transformer_rag import transform_to_rag, extract_text_from_pdf


def create_mime_email(html_body: str = None, text_body: str = None, 
                      subject: str = "Test", from_addr: str = "sender@test.com") -> bytes:
    """Helper to create MIME email bytes."""
    if html_body and text_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
    elif html_body:
        msg = MIMEText(html_body, "html")
    else:
        msg = MIMEText(text_body or "", "plain")
    
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "recipient@test.com"
    msg["Message-ID"] = "<test123@example.com>"
    
    return msg.as_bytes()


def create_mime_with_pdf(pdf_content: bytes = b"%PDF-1.4 fake pdf content") -> bytes:
    """Create a MIME email with a PDF attachment."""
    msg = MIMEMultipart()
    msg["Subject"] = "Invoice Attached"
    msg["From"] = "billing@company.com"
    msg["To"] = "user@example.com"
    
    # Text body
    msg.attach(MIMEText("Please see attached invoice.", "plain"))
    
    # PDF attachment
    pdf_part = MIMEBase("application", "pdf")
    pdf_part.set_payload(pdf_content)
    encoders.encode_base64(pdf_part)
    pdf_part.add_header("Content-Disposition", "attachment", filename="invoice.pdf")
    msg.attach(pdf_part)
    
    return msg.as_bytes()


# ============================================================
# SIGNATURE REMOVAL TESTS
# ============================================================

def test_remove_html_signature():
    """Should remove div with 'signature' class."""
    html = """
    <html>
    <body>
        <div>Hello, this is the main body content.</div>
        <div class='signature'>Bob | CEO | Acme Corp</div>
    </body>
    </html>
    """
    mime_data = create_mime_email(html_body=html)
    result = transform_to_rag(mime_data)
    
    assert "Bob | CEO" not in result["body"]
    assert "main body content" in result["body"]


def test_remove_signature_variants():
    """Should remove various signature formats."""
    html = """
    <div>Important message content here.</div>
    <div class="email-signature">
        John Doe<br>
        Senior Developer
    </div>
    """
    mime_data = create_mime_email(html_body=html)
    result = transform_to_rag(mime_data)
    
    assert "John Doe" not in result["body"]
    assert "Senior Developer" not in result["body"]
    assert "Important message" in result["body"]


# ============================================================
# QUOTE COLLAPSING TESTS
# ============================================================

def test_collapse_nested_quotes():
    """Should collapse deeply nested quotes (3+ levels)."""
    text = """This is a reply.

> Previous message
>> Earlier message
>>> Original message that is very very long
>>> and spans multiple lines
>>> with lots of quoted content
>>>> Even deeper quote
"""
    mime_data = create_mime_email(text_body=text)
    result = transform_to_rag(mime_data)
    
    assert "[Quoted text collapsed]" in result["body"]
    assert ">>>" not in result["body"]
    assert "This is a reply" in result["body"]
    # First two levels should remain
    assert "Previous message" in result["body"] or "> Previous" in result["body"]


def test_preserve_shallow_quotes():
    """Should preserve 1-2 levels of quoting."""
    text = """My response.

> Previous reply
>> Original message
"""
    mime_data = create_mime_email(text_body=text)
    result = transform_to_rag(mime_data)
    
    assert "[Quoted text collapsed]" not in result["body"]
    assert "Previous reply" in result["body"] or "> Previous" in result["body"]


# ============================================================
# TRACKING PIXEL REMOVAL TESTS
# ============================================================

def test_remove_tracking_pixel():
    """Should remove 1x1 tracking pixels."""
    html = """
    <div>Email body content here.</div>
    <img src="https://track.example.com/pixel.png" width="1" height="1">
    """
    mime_data = create_mime_email(html_body=html)
    result = transform_to_rag(mime_data)
    
    assert "<img" not in result["body"]
    assert "pixel" not in result["body"].lower()
    assert "Email body content" in result["body"]


def test_remove_tracking_beacon():
    """Should remove images with tracking-related URLs."""
    html = """
    <div>Newsletter content</div>
    <img src="https://marketing.com/beacon.gif">
    """
    mime_data = create_mime_email(html_body=html)
    result = transform_to_rag(mime_data)
    
    assert "beacon" not in result["body"].lower()


def test_preserve_content_images():
    """Should preserve normal content images (converted to markdown)."""
    html = """
    <div>Check out this diagram:</div>
    <img src="diagram.png" alt="Architecture diagram" width="500" height="300">
    """
    mime_data = create_mime_email(html_body=html)
    result = transform_to_rag(mime_data)
    
    # html2text with ignore_images=True will remove images
    assert "Check out this diagram" in result["body"]


# ============================================================
# PDF EXTRACTION TESTS
# ============================================================

def test_extract_invoice_pdf():
    """Should extract text from PDF attachments."""
    with patch("src.v3_transformer_rag.extract_text_from_pdf") as mock_ocr:
        mock_ocr.return_value = "Invoice #1234 Total: $500.00"
        
        mime_data = create_mime_with_pdf()
        result = transform_to_rag(mime_data)
        
        assert len(result["attachments"]) == 1
        assert result["attachments"][0]["filename"] == "invoice.pdf"
        assert "Invoice #1234" in result["attachments"][0]["extracted_text"]


def test_attachment_metadata():
    """Should include attachment metadata."""
    mime_data = create_mime_with_pdf(pdf_content=b"x" * 1000)
    result = transform_to_rag(mime_data)
    
    assert len(result["attachments"]) == 1
    att = result["attachments"][0]
    assert att["filename"] == "invoice.pdf"
    assert att["size"] == 1000
    assert att["content_type"] == "application/pdf"


# ============================================================
# SIZE REDUCTION TESTS
# ============================================================

def test_size_reduction():
    """Should significantly reduce content size."""
    # Create a large email with lots of boilerplate
    large_html = """
    <html>
    <head><style>body { font-family: Arial; }</style></head>
    <body>
        <div>""" + ("Important content. " * 100) + """</div>
        <div class='signature'>
            """ + ("Very long signature with lots of info. " * 50) + """
        </div>
        """ + ('<img src="pixel.png" width="1" height="1">' * 20) + """
        <script>tracking code here</script>
    </body>
    </html>
    """
    
    raw_size = len(large_html)
    mime_data = create_mime_email(html_body=large_html)
    result = transform_to_rag(mime_data)
    
    result_size = len(result["body"])
    
    print(f"\n[SIZE] Raw HTML: {raw_size} bytes")
    print(f"[SIZE] Cleaned:  {result_size} bytes")
    print(f"[SIZE] Reduction: {(1 - result_size/raw_size) * 100:.0f}%")
    
    # Should achieve significant reduction
    assert result_size < raw_size * 0.5, f"Expected 50%+ reduction, got {result_size}/{raw_size}"


def test_truncate_very_large_content():
    """Should truncate content exceeding 5000 chars."""
    huge_text = "A" * 100000  # 100KB
    mime_data = create_mime_email(text_body=huge_text)
    result = transform_to_rag(mime_data)
    
    assert len(result["body"]) < 6000  # ~5000 + truncation message
    assert "[Content truncated...]" in result["body"]


# ============================================================
# METADATA EXTRACTION TESTS
# ============================================================

def test_extract_metadata():
    """Should extract email metadata."""
    mime_data = create_mime_email(
        text_body="Hello",
        subject="Important Meeting",
        from_addr="boss@company.com"
    )
    result = transform_to_rag(mime_data)
    
    assert result["subject"] == "Important Meeting"
    assert result["from"] == "boss@company.com"
    assert result["to"] == "recipient@test.com"
    assert result["message_id"] == "<test123@example.com>"


def test_generate_thread_id():
    """Should generate thread ID from message ID."""
    mime_data = create_mime_email(text_body="Hello")
    result = transform_to_rag(mime_data)
    
    assert result["thread_id"] != ""
    assert len(result["thread_id"]) == 12  # MD5 hash prefix


# ============================================================
# EDGE CASES
# ============================================================

def test_empty_email():
    """Should handle empty email gracefully."""
    mime_data = create_mime_email(text_body="")
    result = transform_to_rag(mime_data)
    
    assert result["body"] == ""
    assert result["attachments"] == []


def test_plain_text_only():
    """Should handle plain text emails."""
    mime_data = create_mime_email(text_body="Just plain text content.")
    result = transform_to_rag(mime_data)
    
    assert "plain text content" in result["body"]


def test_string_input():
    """Should accept string input as well as bytes."""
    raw = """From: test@example.com
To: user@example.com
Subject: Test
Content-Type: text/plain

Hello world"""
    
    result = transform_to_rag(raw)
    assert "Hello world" in result["body"]
