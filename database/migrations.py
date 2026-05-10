"""
Database migrations
===================
Idempotent ``CREATE TABLE IF NOT EXISTS`` statements executed once at startup.
Add new migration functions here and call them from ``run_migrations``.
"""

from __future__ import annotations

import logging

import aiosqlite

from database.connection import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL statements
# ---------------------------------------------------------------------------

_CREATE_CASES = """
CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    thread_id       TEXT NOT NULL UNIQUE,
    client_email    TEXT NOT NULL,
    subject         TEXT,
    status          TEXT NOT NULL DEFAULT 'received',
    issue_type      TEXT,
    severity        TEXT,
    raw_body        TEXT,
    attachment_text TEXT,
    metadata_json   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

_CREATE_DRAFTS = """
CREATE TABLE IF NOT EXISTS drafts (
    id           TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL REFERENCES cases(id),
    draft_body   TEXT,
    evidence_json TEXT,
    approved_by  TEXT,
    approved_at  TEXT,
    sent_at      TEXT,
    created_at   TEXT NOT NULL
);
"""

_CREATE_AGENT_LOGS = """
CREATE TABLE IF NOT EXISTS agent_logs (
    id              TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES cases(id),
    step            TEXT NOT NULL,
    input_json      TEXT,
    output_json     TEXT,
    tool_calls_json TEXT,
    duration_ms     INTEGER,
    created_at      TEXT NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_migrations() -> None:
    """
    Execute all DDL statements against the configured SQLite database.

    This is safe to call on every startup — all statements use
    ``IF NOT EXISTS`` so they are fully idempotent.
    """
    logger.info("Running database migrations …")
    async with get_db() as db:
        await db.execute(_CREATE_CASES)
        await db.execute(_CREATE_DRAFTS)
        await db.execute(_CREATE_AGENT_LOGS)
    logger.info("Database migrations complete.")
