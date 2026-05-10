"""
Triage agent helper
===================
Defines the ``TriageResult`` dataclass and ``apply_triage_result``, which
persists a classification decision to the database and writes an agent log row.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class TriageResult:
    """Classification output from the ``classify_issue`` Gemini tool call."""

    issue_type: str
    severity: str
    affected_service: str
    confidence: float
    reasoning: str


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def apply_triage_result(
    case_id: str,
    result: TriageResult,
    db: Any,
    *,
    input_json: str = "",
    output_json: str = "",
    tool_calls_json: str = "",
    duration_ms: int = 0,
) -> None:
    """
    Persist a triage classification to the ``cases`` and ``agent_logs`` tables.

    Parameters
    ----------
    case_id:
        UUID of the case being updated.
    result:
        The ``TriageResult`` produced from Gemini's ``classify_issue`` tool call.
    db:
        An open ``aiosqlite`` connection (or compatible async DB handle).
    input_json:
        Serialised input sent to Gemini — stored for observability.
    output_json:
        Serialised output from Gemini — stored for observability.
    tool_calls_json:
        Raw tool-use blocks from the API response — stored for observability.
    duration_ms:
        Wall-clock time the Gemini call took, in milliseconds.
    """
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        """
        UPDATE cases
        SET issue_type = ?, severity = ?, updated_at = ?
        WHERE id = ?
        """,
        (result.issue_type, result.severity, now, case_id),
    )

    log_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO agent_logs
            (id, case_id, step, input_json, output_json, tool_calls_json,
             duration_ms, created_at)
        VALUES (?, ?, 'triage', ?, ?, ?, ?, ?)
        """,
        (
            log_id,
            case_id,
            input_json,
            output_json,
            tool_calls_json,
            duration_ms,
            now,
        ),
    )

    logger.info(
        "Triage applied to case %s: issue_type=%s severity=%s confidence=%.2f",
        case_id,
        result.issue_type,
        result.severity,
        result.confidence,
    )
