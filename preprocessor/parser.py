"""
Email payload parser
====================
Converts a raw Gmail API ``message`` dict (``format='full'``) into a
``CaseInput`` dataclass.

Key behaviours
--------------
- Recursively traverses ``parts`` to handle arbitrarily nested multipart
  messages.
- Prefers ``text/plain`` body parts over ``text/html``.  If only HTML is
  found, tags are stripped with the stdlib ``html.parser``.
- Attachment data is decoded from Gmail's base64url encoding and dispatched
  to ``preprocessor.attachments.extract_attachment``.
- All CPU-bound extraction (OCR, PDF) is run in a ``ThreadPoolExecutor`` via
  ``asyncio.run_in_executor`` so the event loop is never blocked.
- A failed attachment extraction logs the error but does not interrupt
  processing of the rest of the email.

``parse_message`` is **synchronous** and designed to be called from
``asyncio.to_thread`` in the poller.  ``parse_message_async`` is the async
wrapper that also fans out the attachment extractions concurrently.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any

from models.case import AttachmentResult, CaseInput
from preprocessor.attachments import extract_attachment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_message(full_msg: dict[str, Any]) -> CaseInput:
    """
    Parse a Gmail API message dict into a ``CaseInput``.

    Attachment data bytes are collected but OCR / PDF extraction is **not**
    performed here (it would block).  The caller should use
    ``parse_message_async`` when running in an async context to get concurrent
    attachment processing.

    Parameters
    ----------
    full_msg:
        A Gmail message resource with ``format='full'``.

    Returns
    -------
    CaseInput
        Fully populated case input, with attachments extracted synchronously.
    """
    headers = _headers_dict(full_msg)
    thread_id: str = full_msg["threadId"]
    msg_id: str = full_msg["id"]

    client_email = _extract_sender_email(headers.get("From", ""))
    subject = headers.get("Subject", "(no subject)")
    received_at = _parse_date(headers.get("Date", ""))

    payload = full_msg.get("payload", {})
    plain_parts: list[str] = []
    html_parts: list[str] = []
    raw_attachments: list[tuple[str, str, bytes]] = []  # (filename, content_type, data)

    _walk_parts(payload, plain_parts, html_parts, raw_attachments)

    body_plain = "\n\n".join(plain_parts).strip()
    body_html = "\n\n".join(html_parts).strip()

    if body_plain:
        body_text = body_plain
    elif body_html:
        body_text = _strip_html(body_html)
    else:
        body_text = ""

    # Extract attachments synchronously (for callers that can't await).
    attachment_results: list[AttachmentResult] = []
    for filename, content_type, data in raw_attachments:
        result = extract_attachment(filename, content_type, data)
        attachment_results.append(result)

    return CaseInput(
        thread_id=thread_id,
        message_ids=[msg_id],
        client_email=client_email,
        subject=subject,
        body_text=body_text,
        attachment_texts=attachment_results,
        received_at=received_at,
    )


async def parse_message_async(full_msg: dict[str, Any]) -> CaseInput:
    """
    Async version of ``parse_message`` that runs attachment extraction
    concurrently in a ``ThreadPoolExecutor``.

    Parameters
    ----------
    full_msg:
        A Gmail message resource with ``format='full'``.

    Returns
    -------
    CaseInput
        Fully populated case input with all attachments extracted.
    """
    headers = _headers_dict(full_msg)
    thread_id: str = full_msg["threadId"]
    msg_id: str = full_msg["id"]

    client_email = _extract_sender_email(headers.get("From", ""))
    subject = headers.get("Subject", "(no subject)")
    received_at = _parse_date(headers.get("Date", ""))

    payload = full_msg.get("payload", {})
    plain_parts: list[str] = []
    html_parts: list[str] = []
    raw_attachments: list[tuple[str, str, bytes]] = []

    _walk_parts(payload, plain_parts, html_parts, raw_attachments)

    body_plain = "\n\n".join(plain_parts).strip()
    body_html = "\n\n".join(html_parts).strip()

    if body_plain:
        body_text = body_plain
    elif body_html:
        body_text = _strip_html(body_html)
    else:
        body_text = ""

    # Fan-out attachment extraction concurrently.
    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(None, extract_attachment, filename, content_type, data)
        for filename, content_type, data in raw_attachments
    ]
    attachment_results: list[AttachmentResult] = list(await asyncio.gather(*tasks))

    return CaseInput(
        thread_id=thread_id,
        message_ids=[msg_id],
        client_email=client_email,
        subject=subject,
        body_text=body_text,
        attachment_texts=attachment_results,
        received_at=received_at,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _walk_parts(
    part: dict[str, Any],
    plain_parts: list[str],
    html_parts: list[str],
    raw_attachments: list[tuple[str, str, bytes]],
) -> None:
    """
    Recursively traverse a Gmail ``part`` tree, collecting body text and
    raw attachment bytes.

    Parameters
    ----------
    part:
        A Gmail message part (the top-level ``payload`` or any nested part).
    plain_parts / html_parts:
        Accumulate decoded body text strings.
    raw_attachments:
        Accumulates ``(filename, content_type, bytes)`` tuples.
    """
    mime_type: str = part.get("mimeType", "")
    body: dict[str, Any] = part.get("body", {})
    sub_parts: list[dict[str, Any]] = part.get("parts", [])

    # Recurse into multipart containers.
    if mime_type.startswith("multipart/"):
        for sub in sub_parts:
            _walk_parts(sub, plain_parts, html_parts, raw_attachments)
        return

    # Body parts.
    if mime_type == "text/plain" and not _is_attachment(part):
        data = _decode_body_data(body.get("data", ""))
        if data:
            plain_parts.append(data.decode("utf-8", errors="replace"))
        return

    if mime_type == "text/html" and not _is_attachment(part):
        data = _decode_body_data(body.get("data", ""))
        if data:
            html_parts.append(data.decode("utf-8", errors="replace"))
        return

    # Attachment parts.
    filename = _get_filename(part)
    if filename or _is_attachment(part):
        attachment_data = _resolve_attachment_data(body)
        if attachment_data:
            raw_attachments.append((filename or "unknown", mime_type, attachment_data))


def _is_attachment(part: dict[str, Any]) -> bool:
    """Return ``True`` if the part has a Content-Disposition of ``attachment``."""
    for header in part.get("headers", []):
        if header.get("name", "").lower() == "content-disposition":
            return "attachment" in header.get("value", "").lower()
    return bool(_get_filename(part))


def _get_filename(part: dict[str, Any]) -> str:
    """Extract the filename from a part's headers or ``body.attachmentId``."""
    for header in part.get("headers", []):
        if header.get("name", "").lower() == "content-disposition":
            for token in header.get("value", "").split(";"):
                token = token.strip()
                if token.lower().startswith("filename="):
                    return token[9:].strip().strip('"')
    # Fallback: check Content-Type name parameter.
    for header in part.get("headers", []):
        if header.get("name", "").lower() == "content-type":
            for token in header.get("value", "").split(";"):
                token = token.strip()
                if token.lower().startswith("name="):
                    return token[5:].strip().strip('"')
    return ""


def _resolve_attachment_data(body: dict[str, Any]) -> bytes | None:
    """
    Return raw attachment bytes from the body dict.
    Inline data (``body.data``) is preferred; ``attachmentId`` is not resolved
    here (would require a second API call — the poller fetches ``format=full``
    which includes inline data for most attachments).
    """
    if body.get("data"):
        return _decode_body_data(body["data"])
    return None


def _decode_body_data(b64url_data: str) -> bytes:
    """Decode Gmail's base64url-encoded body data."""
    # Gmail uses URL-safe base64 without padding.
    padded = b64url_data + "=" * (4 - len(b64url_data) % 4)
    return base64.urlsafe_b64decode(padded)


def _headers_dict(full_msg: dict[str, Any]) -> dict[str, str]:
    """Flatten the Gmail headers list into a plain dict (last value wins)."""
    result: dict[str, str] = {}
    for header in full_msg.get("payload", {}).get("headers", []):
        result[header["name"]] = header["value"]
    return result


def _extract_sender_email(from_header: str) -> str:
    """Parse ``From: "Name" <email>`` and return the email address part."""
    _, addr = parseaddr(from_header)
    return addr or from_header


def _parse_date(date_str: str) -> datetime:
    """
    Parse an RFC2822 date string.  Falls back to UTC now on failure.
    """
    from email.utils import parsedate_to_datetime

    if not date_str:
        return datetime.now(timezone.utc)
    try:
        return parsedate_to_datetime(date_str)
    except Exception:  # noqa: BLE001
        logger.warning("Could not parse date %r — using UTC now.", date_str)
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# HTML tag stripper
# ---------------------------------------------------------------------------


class _HTMLStripper(HTMLParser):
    """Minimal HTMLParser subclass that collects non-tag text."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    """Strip HTML tags from a string using the stdlib parser."""
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text().strip()
