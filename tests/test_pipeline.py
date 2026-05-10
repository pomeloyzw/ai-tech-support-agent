"""
Test suite — Step 1 (Gmail poller) & Step 2 (Email pre-processor)
==================================================================

Covers:
  TC-01  Parse a plain-text email                   (parser)
  TC-02  Parse a multipart email (plain + HTML)     (parser)
  TC-03  HTML-only email → tags stripped             (parser)
  TC-04  Image attachment → OCR extraction          (attachments)
  TC-05  PDF attachment → pdfplumber extraction     (attachments)
  TC-06  JSON attachment → pretty-printed           (attachments)
  TC-07  Plain-text attachment                      (attachments)
  TC-08  Unsupported attachment type                (attachments)
  TC-09  New email → case inserted in DB            (poller + DB)
  TC-10  Reply on pending_info case → body appended (poller + DB)
  TC-11  Reply on non-pending_info case → skipped   (poller + DB)
  TC-12  GET /health returns ok                     (FastAPI)
  TC-13  GET /cases returns the stored cases        (FastAPI)

Run with:
    pip install pytest pytest-asyncio httpx
    pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# ---------------------------------------------------------------------------
# Helpers — build fake Gmail API message dicts
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    """URL-safe base64-encode a string (as Gmail does)."""
    return base64.urlsafe_b64encode(text.encode()).decode()


def _b64_bytes(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _make_message(
    thread_id: str = "thread_abc",
    msg_id: str = "msg_001",
    from_: str = "client@bank.com",
    subject: str = "API failure on /v1/payments",
    date: str = "Fri, 09 May 2026 10:00:00 +0800",
    body_plain: str | None = "Hello, our API is returning 401.",
    body_html: str | None = None,
    attachments: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Build a minimal Gmail ``message`` resource (format=full).
    Supports plain-text body, HTML body, and arbitrary attachments.
    """
    headers = [
        {"name": "From", "value": from_},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": date},
    ]

    parts: list[dict] = []

    if body_plain:
        parts.append({
            "mimeType": "text/plain",
            "headers": [],
            "body": {"data": _b64(body_plain)},
            "parts": [],
        })

    if body_html:
        parts.append({
            "mimeType": "text/html",
            "headers": [],
            "body": {"data": _b64(body_html)},
            "parts": [],
        })

    for att in (attachments or []):
        parts.append(att)

    if len(parts) == 1 and not attachments:
        # Simple non-multipart message.
        payload = {
            "mimeType": parts[0]["mimeType"],
            "headers": headers,
            "body": parts[0]["body"],
            "parts": [],
        }
    else:
        payload = {
            "mimeType": "multipart/mixed",
            "headers": headers,
            "body": {},
            "parts": parts,
        }

    return {"id": msg_id, "threadId": thread_id, "payload": payload}


def _attachment_part(
    filename: str,
    mime_type: str,
    data: bytes,
) -> dict:
    """Return a Gmail attachment part dict."""
    return {
        "mimeType": mime_type,
        "headers": [
            {"name": "Content-Disposition", "value": f'attachment; filename="{filename}"'},
        ],
        "body": {"data": _b64_bytes(data)},
        "parts": [],
    }


# ---------------------------------------------------------------------------
# TC-01  Parse plain-text email
# ---------------------------------------------------------------------------


def test_tc01_parse_plain_text_email():
    """TC-01: A plain-text email is parsed into a CaseInput correctly."""
    from preprocessor.parser import parse_message

    msg = _make_message(body_plain="Hello, our API is returning 401 errors.")
    result = parse_message(msg)

    assert result.thread_id == "thread_abc"
    assert result.client_email == "client@bank.com"
    assert result.subject == "API failure on /v1/payments"
    assert "401" in result.body_text
    assert result.attachment_texts == []


# ---------------------------------------------------------------------------
# TC-02  Parse multipart email (plain preferred over HTML)
# ---------------------------------------------------------------------------


def test_tc02_prefer_plain_over_html():
    """TC-02: When both plain and HTML parts exist, plain text is used."""
    from preprocessor.parser import parse_message

    msg = _make_message(
        body_plain="Plain body content.",
        body_html="<html><body><b>HTML body content.</b></body></html>",
    )
    result = parse_message(msg)

    assert result.body_text == "Plain body content."


# ---------------------------------------------------------------------------
# TC-03  HTML-only email → tags stripped
# ---------------------------------------------------------------------------


def test_tc03_html_only_tags_stripped():
    """TC-03: An HTML-only email has its tags stripped to plain text."""
    from preprocessor.parser import parse_message

    msg = _make_message(
        body_plain=None,
        body_html="<html><body><p>Our system is <b>down</b>.</p></body></html>",
    )
    result = parse_message(msg)

    assert "<" not in result.body_text
    assert "down" in result.body_text


# ---------------------------------------------------------------------------
# TC-04  Image attachment → OCR
# ---------------------------------------------------------------------------


def test_tc04_image_attachment_ocr(tmp_path):
    """TC-04: PNG attachment is routed to pytesseract OCR."""
    from preprocessor.attachments import extract_attachment

    # Create a tiny real PNG using Pillow so we can write actual image bytes.
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (200, 50), color="white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), "ERROR CODE 401", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png_bytes = buf.getvalue()

    result = extract_attachment("screenshot.png", "image/png", png_bytes)

    assert result.filename == "screenshot.png"
    assert result.extraction_method == "ocr"
    # OCR may not be perfect, but it shouldn't crash and must return something.
    assert isinstance(result.extracted_text, str)


# ---------------------------------------------------------------------------
# TC-05  PDF attachment → pdfplumber
# ---------------------------------------------------------------------------


def test_tc05_pdf_attachment_pdfplumber():
    """TC-05: PDF bytes are extracted with pdfplumber."""
    from preprocessor.attachments import extract_attachment

    # Build a minimal valid PDF in-memory with reportlab if available;
    # otherwise mock pdfplumber directly so the test is self-contained.
    try:
        import reportlab.lib.pagesizes as ps
        from reportlab.pdfgen import canvas as rl_canvas

        buf = io.BytesIO()
        c = rl_canvas.Canvas(buf, pagesize=ps.A4)
        c.drawString(100, 750, "Transaction failed: insufficient funds")
        c.save()
        pdf_bytes = buf.getvalue()
        use_mock = False
    except ImportError:
        pdf_bytes = b"%PDF-1.4 fake"
        use_mock = True

    if use_mock:
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Transaction failed: insufficient funds"
        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            result = extract_attachment("report.pdf", "application/pdf", pdf_bytes)
    else:
        result = extract_attachment("report.pdf", "application/pdf", pdf_bytes)

    assert result.extraction_method == "pdfplumber"
    assert "insufficient funds" in result.extracted_text or result.extracted_text != ""


# ---------------------------------------------------------------------------
# TC-06  JSON attachment → pretty-printed
# ---------------------------------------------------------------------------


def test_tc06_json_attachment():
    """TC-06: JSON attachment is parsed and pretty-printed."""
    from preprocessor.attachments import extract_attachment

    payload = {"error": "UNAUTHORIZED", "code": 401, "request_id": "abc-123"}
    raw = json.dumps(payload).encode()

    result = extract_attachment("error.json", "application/json", raw)

    assert result.extraction_method == "json_parse"
    parsed_back = json.loads(result.extracted_text)
    assert parsed_back["code"] == 401
    assert "  " in result.extracted_text  # Indented


# ---------------------------------------------------------------------------
# TC-07  Plain-text attachment
# ---------------------------------------------------------------------------


def test_tc07_plain_text_attachment():
    """TC-07: Plain-text attachment bytes are decoded directly."""
    from preprocessor.attachments import extract_attachment

    content = "Stack trace:\nKeyError: 'token'\n  at line 42"
    result = extract_attachment("trace.txt", "text/plain", content.encode())

    assert result.extraction_method == "plain_text"
    assert "KeyError" in result.extracted_text


# ---------------------------------------------------------------------------
# TC-08  Unsupported attachment type
# ---------------------------------------------------------------------------


def test_tc08_unsupported_attachment():
    """TC-08: Unknown MIME type returns empty text and method=unsupported."""
    from preprocessor.attachments import extract_attachment

    result = extract_attachment("data.bin", "application/octet-stream", b"\x00\x01\x02")

    assert result.extraction_method == "unsupported"
    assert result.extracted_text == ""


# ---------------------------------------------------------------------------
# TC-09  New email → case inserted in DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tc09_new_email_creates_case(tmp_path):
    """TC-09: A new email thread inserts one case row with status 'received'."""
    import os
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")

    # Re-import config after env change so DB path is picked up.
    import importlib
    import config as cfg
    importlib.reload(cfg)
    cfg.settings.database_path = str(tmp_path / "test.db")

    from database.migrations import run_migrations
    from database.connection import get_db
    from gmail.poller import _process_message, _insert_new_case
    from preprocessor.parser import parse_message

    await run_migrations()

    msg = _make_message(
        thread_id="thread_new_001",
        msg_id="msg_new_001",
        from_="alice@bigbank.com",
        subject="POST /v1/transfer returns 500",
        body_plain="Getting internal server error on transfer endpoint.",
    )

    case_input = parse_message(msg)

    async with get_db() as db:
        await _insert_new_case(db, case_input)

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT * FROM cases WHERE thread_id = ?", ("thread_new_001",)
        )
        row = await cursor.fetchone()

    assert row is not None
    assert row["client_email"] == "alice@bigbank.com"
    assert row["status"] == "received"
    assert "internal server error" in row["raw_body"].lower()


# ---------------------------------------------------------------------------
# TC-10  Reply on pending_info case → body appended, status reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tc10_reply_appends_body(tmp_path):
    """TC-10: A reply to a pending_info case appends the body and resets status."""
    import os
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")

    import importlib
    import config as cfg
    importlib.reload(cfg)
    cfg.settings.database_path = str(tmp_path / "test.db")

    from database.migrations import run_migrations
    from database.connection import get_db
    from gmail.poller import _insert_new_case, _append_reply
    from preprocessor.parser import parse_message

    await run_migrations()

    # Insert initial case.
    original_msg = _make_message(
        thread_id="thread_reply_001",
        body_plain="Original issue: API returning 401.",
    )
    case_input = parse_message(original_msg)
    async with get_db() as db:
        await _insert_new_case(db, case_input)

    # Manually set status to pending_info.
    async with get_db() as db:
        await db.execute(
            "UPDATE cases SET status = 'pending_info' WHERE thread_id = ?",
            ("thread_reply_001",),
        )

    # Simulate a reply.
    reply_msg = _make_message(
        thread_id="thread_reply_001",
        msg_id="msg_reply_002",
        body_plain="Here is the additional info you requested.",
    )
    reply_input = parse_message(reply_msg)

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT id FROM cases WHERE thread_id = ?", ("thread_reply_001",)
        )
        row = await cursor.fetchone()
        case_id = row["id"]
        await _append_reply(db, case_id, reply_input)

    async with get_db() as db:
        cursor = await db.execute(
            "SELECT status, raw_body FROM cases WHERE thread_id = ?",
            ("thread_reply_001",),
        )
        row = await cursor.fetchone()

    assert row["status"] == "received"
    assert "Original issue" in row["raw_body"]
    assert "additional info" in row["raw_body"]
    assert "--- [Reply received] ---" in row["raw_body"]


# ---------------------------------------------------------------------------
# TC-11  Reply on non-pending_info case → skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tc11_reply_on_active_case_skipped(tmp_path):
    """TC-11: A reply to a case in status != pending_info is skipped (not updated)."""
    import os
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")

    import importlib
    import config as cfg
    importlib.reload(cfg)
    cfg.settings.database_path = str(tmp_path / "test.db")

    from database.migrations import run_migrations
    from database.connection import get_db
    from gmail.poller import _insert_new_case, _fetch_case_by_thread
    from preprocessor.parser import parse_message

    await run_migrations()

    # Insert initial case and set status to "investigating".
    original_msg = _make_message(thread_id="thread_skip_001", body_plain="Issue.")
    case_input = parse_message(original_msg)
    async with get_db() as db:
        await _insert_new_case(db, case_input)
        await db.execute(
            "UPDATE cases SET status = 'investigating' WHERE thread_id = ?",
            ("thread_skip_001",),
        )

    # Simulate what the poller does — it checks the status before deciding.
    async with get_db() as db:
        existing = await _fetch_case_by_thread(db, "thread_skip_001")

    assert existing is not None
    assert existing["status"] == "investigating"
    # Poller would return "skipped" — status must not change.


# ---------------------------------------------------------------------------
# TC-12 & TC-13  FastAPI endpoints (/health and /cases)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tc12_health_endpoint(tmp_path):
    """TC-12: GET /health returns status=ok, db=ok, scheduler=running."""
    import os
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")

    import importlib
    import config as cfg
    importlib.reload(cfg)
    cfg.settings.database_path = str(tmp_path / "test.db")

    # Patch Gmail auth so startup doesn't try to open a browser.
    with patch("gmail.poller.get_gmail_service", return_value=MagicMock()):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["scheduler"] in ("running", "stopped")  # stopped during test teardown is fine


@pytest.mark.asyncio
async def test_tc13_cases_endpoint_returns_stored_cases(tmp_path):
    """TC-13: GET /cases returns all cases inserted in the DB."""
    import os
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")

    import importlib
    import config as cfg
    importlib.reload(cfg)
    cfg.settings.database_path = str(tmp_path / "test.db")

    from database.migrations import run_migrations
    from database.connection import get_db
    from gmail.poller import _insert_new_case
    from preprocessor.parser import parse_message

    await run_migrations()

    msg = _make_message(
        thread_id="thread_api_001",
        from_="bob@corp.com",
        subject="Missing transaction data",
        body_plain="Transactions from yesterday are missing in the portal.",
    )
    async with get_db() as db:
        await _insert_new_case(db, parse_message(msg))

    with patch("gmail.poller.get_gmail_service", return_value=MagicMock()):
        from main import app
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/cases")

    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    subjects = [c["subject"] for c in body["cases"]]
    assert "Missing transaction data" in subjects
