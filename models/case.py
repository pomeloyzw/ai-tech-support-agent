"""
Case data models
================
Dataclasses used throughout the pipeline.  ``CaseInput`` represents an email
that has been parsed from the raw Gmail payload.  ``Case`` mirrors the
``cases`` table row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


# ---------------------------------------------------------------------------
# Input / pre-processed models
# ---------------------------------------------------------------------------


@dataclass
class AttachmentResult:
    """Text extracted from a single email attachment."""

    filename: str
    content_type: str
    extracted_text: str
    extraction_method: Literal[
        "ocr", "pdfplumber", "json_parse", "plain_text", "unsupported"
    ]


@dataclass
class CaseInput:
    """Structured representation of one Gmail thread ready for persistence."""

    thread_id: str
    message_ids: list[str]
    client_email: str
    subject: str
    body_text: str
    attachment_texts: list[AttachmentResult] = field(default_factory=list)
    received_at: datetime = field(default_factory=datetime.utcnow)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def concatenated_attachment_text(self) -> str:
        """Return all attachment extracted texts joined by a separator."""
        return "\n\n---\n\n".join(
            a.extracted_text for a in self.attachment_texts if a.extracted_text
        )

    def metadata_json(self) -> str:
        """Return a JSON blob capturing envelope metadata for storage."""
        return json.dumps(
            {
                "sender": self.client_email,
                "timestamp": self.received_at.isoformat(),
                "message_ids": self.message_ids,
                "subject": self.subject,
            },
            ensure_ascii=False,
        )


# ---------------------------------------------------------------------------
# DB row model
# ---------------------------------------------------------------------------

CaseStatus = Literal[
    "received", "pending_info", "investigating", "draft_ready", "sent"
]


@dataclass
class Case:
    """Mirrors one row in the ``cases`` table."""

    id: str
    thread_id: str
    client_email: str
    subject: str | None
    status: CaseStatus
    issue_type: str | None
    severity: str | None
    raw_body: str | None
    attachment_text: str | None
    metadata_json: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: tuple) -> "Case":
        """Construct a ``Case`` from a raw aiosqlite row tuple."""
        (
            id_,
            thread_id,
            client_email,
            subject,
            status,
            issue_type,
            severity,
            raw_body,
            attachment_text,
            metadata_json,
            created_at,
            updated_at,
        ) = row
        return cls(
            id=id_,
            thread_id=thread_id,
            client_email=client_email,
            subject=subject,
            status=status,
            issue_type=issue_type,
            severity=severity,
            raw_body=raw_body,
            attachment_text=attachment_text,
            metadata_json=metadata_json,
            created_at=created_at,
            updated_at=updated_at,
        )
