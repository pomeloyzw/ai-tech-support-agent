"""
Gmail poller
============
Polls the Gmail support inbox for unread messages every
``GMAIL_POLL_INTERVAL_SECONDS`` seconds (scheduled by APScheduler).

Algorithm
---------
1. List unread messages addressed to ``GMAIL_SUPPORT_ADDRESS``.
   If ``GMAIL_SENDER_FILTER`` is set, only messages *from* that address
   are considered.
2. For each message, fetch the full payload.
3. Resolve whether the message belongs to a known thread (case) in SQLite.
   - **New thread** → parse + insert a new case (status ``received``).
   - **Reply on ``pending_info`` case** → append body, reset to ``received``.
   - **Reply on any other status** → skip and log.
4. Mark the Gmail message as read (remove ``UNREAD`` label).
5. All Gmail API calls use exponential backoff (max 3 retries) on quota errors.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from googleapiclient.errors import HttpError

from config import settings
from database.connection import get_db
from gmail.auth import get_gmail_service
from models.case import CaseInput
from preprocessor.parser import parse_message

logger = logging.getLogger(__name__)

# Maximum number of retries for quota / transient Gmail API errors.
_MAX_RETRIES = 3
_BACKOFF_BASE = 2  # seconds; delay = base ** attempt


# ---------------------------------------------------------------------------
# Public entry point (called by APScheduler)
# ---------------------------------------------------------------------------


async def poll_inbox() -> None:
    """
    Main poll cycle.  Fetches unread emails, processes each one, and logs
    a summary of the cycle.  Exceptions are caught at the top level so a
    single broken email never stops the scheduler.
    """
    logger.info("Poll cycle starting …")
    try:
        service = get_gmail_service()
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to obtain Gmail service: %s", exc)
        return

    messages = await asyncio.to_thread(_list_unread_messages, service)
    if not messages:
        logger.info("Poll cycle complete — no unread messages.")
        return

    logger.info("Found %d unread message(s) to process.", len(messages))

    new_cases = 0
    updated_cases = 0
    skipped = 0

    for msg_stub in messages:
        msg_id: str = msg_stub["id"]
        try:
            full_msg = await asyncio.to_thread(_fetch_message, service, msg_id)
            result = await _process_message(full_msg)
            if result == "new":
                new_cases += 1
            elif result == "updated":
                updated_cases += 1
            else:
                skipped += 1

            # Mark as read regardless of outcome to avoid reprocessing.
            await asyncio.to_thread(_mark_as_read, service, msg_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Error processing message %s: %s", msg_id, exc, exc_info=True)
            skipped += 1

    logger.info(
        "Poll cycle complete — new=%d updated=%d skipped=%d",
        new_cases,
        updated_cases,
        skipped,
    )


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------


async def _process_message(full_msg: dict[str, Any]) -> str:
    """
    Decide what to do with a fetched Gmail message.

    Returns one of ``"new"``, ``"updated"``, or ``"skipped"``.
    """
    thread_id: str = full_msg["threadId"]

    case_input: CaseInput = await asyncio.to_thread(parse_message, full_msg)

    async with get_db() as db:
        existing = await _fetch_case_by_thread(db, thread_id)

        if existing is None:
            await _insert_new_case(db, case_input)
            logger.info(
                "New case created for thread %s from %s",
                thread_id,
                case_input.client_email,
            )
            return "new"

        case_id: str = existing["id"]
        status: str = existing["status"]

        if status == "pending_info":
            await _append_reply(db, case_id, case_input)
            logger.info(
                "Case %s updated with reply (was pending_info → received).", case_id
            )
            return "updated"

        logger.info(
            "Skipping reply for thread %s — case status is %r (not pending_info).",
            thread_id,
            status,
        )
        return "skipped"


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------


async def _fetch_case_by_thread(
    db: Any, thread_id: str
) -> Any | None:
    """Return the cases row for ``thread_id`` or ``None`` if not found."""
    cursor = await db.execute(
        "SELECT id, status FROM cases WHERE thread_id = ?", (thread_id,)
    )
    return await cursor.fetchone()


async def _insert_new_case(db: Any, case_input: CaseInput) -> None:
    """Insert a fresh case row from a ``CaseInput``."""
    import uuid
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    case_id = str(uuid.uuid4())

    await db.execute(
        """
        INSERT INTO cases
            (id, thread_id, client_email, subject, status,
             raw_body, attachment_text, metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'received', ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            case_input.thread_id,
            case_input.client_email,
            case_input.subject,
            case_input.body_text,
            case_input.concatenated_attachment_text(),
            case_input.metadata_json(),
            now,
            now,
        ),
    )


async def _append_reply(db: Any, case_id: str, case_input: CaseInput) -> None:
    """Append the new message body to an existing case and reset its status."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    separator = "\n\n--- [Reply received] ---\n\n"

    # Fetch existing body to append.
    cursor = await db.execute(
        "SELECT raw_body, attachment_text FROM cases WHERE id = ?", (case_id,)
    )
    row = await cursor.fetchone()
    existing_body: str = (row["raw_body"] or "") if row else ""
    existing_attach: str = (row["attachment_text"] or "") if row else ""

    new_body = existing_body + separator + case_input.body_text
    new_attach_parts = [existing_attach, case_input.concatenated_attachment_text()]
    new_attach = "\n\n---\n\n".join(p for p in new_attach_parts if p)

    await db.execute(
        """
        UPDATE cases
        SET raw_body = ?, attachment_text = ?, status = 'received', updated_at = ?
        WHERE id = ?
        """,
        (new_body, new_attach, now, case_id),
    )


# ---------------------------------------------------------------------------
# Gmail API helpers with retry / backoff
# ---------------------------------------------------------------------------


def _list_unread_messages(service: Any) -> list[dict[str, Any]]:
    """
    Return stub dicts ``{id, threadId}`` for all unread messages in the
    support inbox.  Retries up to ``_MAX_RETRIES`` times on quota errors.
    """
    query_parts = [f"to:{settings.gmail_support_address}", "is:unread"]
    if settings.gmail_sender_filter:
        query_parts.append(f"from:{settings.gmail_sender_filter}")
        logger.debug("Sender filter active: from:%s", settings.gmail_sender_filter)
    query = " ".join(query_parts)
    messages: list[dict[str, Any]] = []

    for attempt in range(_MAX_RETRIES):
        try:
            response = (
                service.users()
                .messages()
                .list(userId="me", q=query, maxResults=100)
                .execute()
            )
            messages = response.get("messages", [])
            return messages
        except HttpError as exc:
            if exc.resp.status in (429, 500, 503) and attempt < _MAX_RETRIES - 1:
                delay = _BACKOFF_BASE ** attempt
                logger.warning(
                    "Gmail API quota/transient error (attempt %d/%d), "
                    "retrying in %ds: %s",
                    attempt + 1,
                    _MAX_RETRIES,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                raise

    return messages


def _fetch_message(service: Any, msg_id: str) -> dict[str, Any]:
    """
    Fetch a full Gmail message by ``msg_id``.
    Retries up to ``_MAX_RETRIES`` times on transient errors.
    """
    for attempt in range(_MAX_RETRIES):
        try:
            return (
                service.users()
                .messages()
                .get(userId="me", id=msg_id, format="full")
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status in (429, 500, 503) and attempt < _MAX_RETRIES - 1:
                delay = _BACKOFF_BASE ** attempt
                logger.warning(
                    "Gmail API error fetching %s (attempt %d/%d), "
                    "retrying in %ds: %s",
                    msg_id,
                    attempt + 1,
                    _MAX_RETRIES,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                raise

    # Unreachable, but makes the type checker happy.
    raise RuntimeError(f"Failed to fetch message {msg_id} after {_MAX_RETRIES} attempts")


def _mark_as_read(service: Any, msg_id: str) -> None:
    """
    Remove the ``UNREAD`` label from a Gmail message.
    Retries up to ``_MAX_RETRIES`` times on transient errors.
    """
    body = {"removeLabelIds": ["UNREAD"]}
    for attempt in range(_MAX_RETRIES):
        try:
            service.users().messages().modify(
                userId="me", id=msg_id, body=body
            ).execute()
            return
        except HttpError as exc:
            if exc.resp.status in (429, 500, 503) and attempt < _MAX_RETRIES - 1:
                delay = _BACKOFF_BASE ** attempt
                logger.warning(
                    "Gmail mark-as-read error for %s (attempt %d/%d), "
                    "retrying in %ds: %s",
                    msg_id,
                    attempt + 1,
                    _MAX_RETRIES,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Failed to mark message %s as read after %d attempts: %s",
                    msg_id,
                    _MAX_RETRIES,
                    exc,
                )
                return  # Non-fatal — don't re-raise
