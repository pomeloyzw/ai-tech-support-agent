"""
Info-gap agent helper
=====================
Defines the ``InfoGapResult`` dataclass and ``apply_infogap_result``, which:
- Advances a case to ``investigating`` when all required information is present.
- Sets the case to ``pending_info`` and creates a ``drafts`` row with the
  follow-up email body when information is missing.
- Writes an ``agent_logs`` row for observability.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class InfoGapResult:
    """Output from the ``check_info_completeness`` Gemini tool call."""

    is_complete: bool
    missing_fields: list[str] = field(default_factory=list)
    follow_up_email_body: str = ""


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def apply_infogap_result(
    case_id: str,
    result: InfoGapResult,
    db: Any,
    *,
    input_json: str = "",
    output_json: str = "",
    tool_calls_json: str = "",
    duration_ms: int = 0,
) -> None:
    """
    Persist an info-gap decision to the database.

    If ``result.is_complete`` is ``True``, the case status is advanced to
    ``investigating``.  Otherwise it is set to ``pending_info`` and a row is
    inserted into the ``drafts`` table with the follow-up email body and the
    list of missing fields stored as ``evidence_json``.

    Parameters
    ----------
    case_id:
        UUID of the case being updated.
    result:
        The ``InfoGapResult`` produced from Gemini's
        ``check_info_completeness`` tool call.
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

    if result.is_complete:
        new_status = "investigating"
        logger.info(
            "Case %s has sufficient information — advancing to 'investigating'.",
            case_id,
        )
    else:
        new_status = "pending_info"
        logger.info(
            "Case %s is missing information (%s) — setting to 'pending_info'.",
            case_id,
            ", ".join(result.missing_fields),
        )

        # Insert a draft row containing the follow-up email body.
        draft_id = str(uuid.uuid4())
        evidence = json.dumps(
            {
                "type": "info_request",
                "missing_fields": result.missing_fields,
            },
            ensure_ascii=False,
        )
        await db.execute(
            """
            INSERT INTO drafts (id, case_id, draft_body, evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (draft_id, case_id, result.follow_up_email_body, evidence, now),
        )

    await db.execute(
        """
        UPDATE cases
        SET status = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_status, now, case_id),
    )

    # Write the agent log entry.
    log_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO agent_logs
            (id, case_id, step, input_json, output_json, tool_calls_json,
             duration_ms, created_at)
        VALUES (?, ?, 'infogap', ?, ?, ?, ?, ?)
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
