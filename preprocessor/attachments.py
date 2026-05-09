"""
Attachment text extractor
=========================
Provides ``extract_attachment`` which dispatches to the correct extraction
strategy based on MIME content-type.

Extraction strategies
---------------------
- **Images** (image/png, image/jpeg, image/gif, image/webp):
  Writes bytes to a temp file, runs ``pytesseract``, deletes the temp file.
- **PDFs** (application/pdf):
  Opens the raw bytes with ``pdfplumber`` and concatenates page text.
- **JSON** (application/json):
  Parses and re-serialises with ``json.dumps(indent=2)`` for readability.
- **Plain text** (text/plain):
  Decodes bytes to UTF-8 (replacing unmappable characters).
- **Anything else**:
  Logs a warning and returns an empty string with method ``"unsupported"``.

All extraction is wrapped in try/except — a failed attachment must not crash
the caller.  Errors are logged and an ``AttachmentResult`` with empty
``extracted_text`` is returned.

Note: ``extract_attachment`` is a synchronous function.  The caller
(``parser.py``) runs it inside ``asyncio.run_in_executor`` so the event loop
is never blocked.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile

import pdfplumber
import pytesseract
from PIL import Image

from models.case import AttachmentResult

logger = logging.getLogger(__name__)

# MIME types that map to specific extraction strategies.
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def extract_attachment(
    filename: str,
    content_type: str,
    data: bytes,
) -> AttachmentResult:
    """
    Extract readable text from an email attachment.

    Parameters
    ----------
    filename:
        Original filename from the Gmail payload (used only for logging).
    content_type:
        MIME type string, e.g. ``"application/pdf"``.
    data:
        Raw bytes of the attachment.

    Returns
    -------
    AttachmentResult
        Always returns a result — errors are caught internally and produce an
        empty ``extracted_text`` with ``extraction_method = "unsupported"``.
    """
    normalized_ct = content_type.split(";")[0].strip().lower()

    try:
        if normalized_ct in _IMAGE_TYPES:
            return _extract_image(filename, normalized_ct, data)
        elif normalized_ct == "application/pdf":
            return _extract_pdf(filename, data)
        elif normalized_ct == "application/json":
            return _extract_json(filename, data)
        elif normalized_ct == "text/plain":
            return _extract_plain_text(filename, data)
        else:
            logger.warning(
                "Unsupported attachment type %r for file %r — skipping.",
                content_type,
                filename,
            )
            return AttachmentResult(
                filename=filename,
                content_type=content_type,
                extracted_text="",
                extraction_method="unsupported",
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to extract attachment %r (%s): %s",
            filename,
            content_type,
            exc,
            exc_info=True,
        )
        return AttachmentResult(
            filename=filename,
            content_type=content_type,
            extracted_text="",
            extraction_method="unsupported",
        )


# ---------------------------------------------------------------------------
# Strategy implementations
# ---------------------------------------------------------------------------


def _extract_image(filename: str, content_type: str, data: bytes) -> AttachmentResult:
    """Run Tesseract OCR on an image attachment."""
    suffix = _suffix_for_content_type(content_type)
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            tmp_path = tmp.name

        text: str = pytesseract.image_to_string(Image.open(tmp_path))
        return AttachmentResult(
            filename=filename,
            content_type=content_type,
            extracted_text=text.strip(),
            extraction_method="ocr",
        )
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _extract_pdf(filename: str, data: bytes) -> AttachmentResult:
    """Extract text from all pages of a PDF using pdfplumber."""
    import io

    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                pages.append(page_text)

    return AttachmentResult(
        filename=filename,
        content_type="application/pdf",
        extracted_text="\n\n".join(pages).strip(),
        extraction_method="pdfplumber",
    )


def _extract_json(filename: str, data: bytes) -> AttachmentResult:
    """Parse JSON and re-serialise with indentation for human readability."""
    raw_text = data.decode("utf-8", errors="replace")
    parsed = json.loads(raw_text)
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    return AttachmentResult(
        filename=filename,
        content_type="application/json",
        extracted_text=pretty,
        extraction_method="json_parse",
    )


def _extract_plain_text(filename: str, data: bytes) -> AttachmentResult:
    """Decode raw bytes as UTF-8 plain text."""
    text = data.decode("utf-8", errors="replace")
    return AttachmentResult(
        filename=filename,
        content_type="text/plain",
        extracted_text=text.strip(),
        extraction_method="plain_text",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _suffix_for_content_type(content_type: str) -> str:
    """Return a file extension for use with ``tempfile.NamedTemporaryFile``."""
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    return mapping.get(content_type, ".bin")
