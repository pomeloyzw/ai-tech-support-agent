"""
AI Technical Support Agent — FastAPI Application
================================================

How to run this project for the first time
------------------------------------------

Prerequisites
~~~~~~~~~~~~~
1. Python 3.11+ installed.
2. Tesseract OCR installed and on PATH:
   - macOS:  ``brew install tesseract``
   - Ubuntu: ``sudo apt install tesseract-ocr``
   - Windows: https://github.com/UB-Mannheim/tesseract/wiki
3. A Google Cloud project with the Gmail API enabled and an OAuth 2.0
   Desktop Client credential downloaded as ``credentials.json`` (placed in
   the project root or wherever ``GMAIL_CREDENTIALS_PATH`` points).

Setup
~~~~~
::

    # 1. Create and activate a virtual environment
    python -m venv .venv
    source .venv/bin/activate          # Windows: .venv\\Scripts\\activate

    # 2. Install dependencies
    pip install -r requirements.txt

    # 3. Copy the example env file and fill in your values
    cp .env.example .env

    # 4. Run the application — on first start, a browser window will open
    #    asking you to authorise Gmail access.  After authorising, the token
    #    is saved to token.json and all future starts are automatic.
    uvicorn main:app --reload

    # The health-check endpoint confirms everything is running:
    curl http://localhost:8000/health

    # Browse persisted cases (for dev/debug):
    curl http://localhost:8000/cases

Notes
~~~~~
- ``support_agent.db`` (SQLite) is created automatically in the project root
  (or wherever ``DATABASE_PATH`` points) on first startup.
- The Gmail poller runs every ``GMAIL_POLL_INTERVAL_SECONDS`` seconds as a
  background APScheduler job and does NOT need manual triggering.
- The ``token.json`` file contains a refresh token — keep it secret and out
  of source control (add to ``.gitignore``).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config import settings
from database.connection import get_db
from database.migrations import run_migrations
from gmail.poller import poll_inbox

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler (module-level so the health endpoint can inspect it)
# ---------------------------------------------------------------------------

_scheduler = AsyncIOScheduler()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan context manager.

    On startup
    ~~~~~~~~~~
    1. Run all DB migrations (idempotent CREATE TABLE IF NOT EXISTS).
    2. Register the Gmail poll job and start the scheduler.

    On shutdown
    ~~~~~~~~~~~
    Gracefully shuts down the scheduler so in-progress jobs finish.
    """
    # ---- Startup ----
    logger.info("Starting AI Technical Support Agent …")
    await run_migrations()

    _scheduler.add_job(
        poll_inbox,
        trigger="interval",
        seconds=settings.gmail_poll_interval_seconds,
        id="gmail_poll",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "Gmail poller scheduled every %ds.",
        settings.gmail_poll_interval_seconds,
    )

    yield

    # ---- Shutdown ----
    logger.info("Shutting down scheduler …")
    _scheduler.shutdown(wait=True)
    logger.info("Scheduler stopped. Goodbye.")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI Technical Support Agent",
    description="Gmail poller + email pre-processor for B2B SaaS support.",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", summary="Health check")
async def health_check() -> JSONResponse:
    """
    Return the operational status of the application.

    Checks
    ------
    - **db**: Attempts a lightweight query against SQLite.
    - **scheduler**: Inspects whether the APScheduler is running.
    """
    db_status = "ok"
    try:
        async with get_db() as db:
            await db.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check DB error: %s", exc)
        db_status = "error"

    scheduler_status = "running" if _scheduler.running else "stopped"

    return JSONResponse(
        content={
            "status": "ok",
            "db": db_status,
            "scheduler": scheduler_status,
        }
    )


@app.get("/cases", summary="List all cases (debug)")
async def list_cases() -> JSONResponse:
    """
    Return all persisted cases ordered by ``created_at DESC``.

    This endpoint is intended for development/debugging only.  It returns
    raw column values; no pagination or filtering is applied.
    """
    rows: list[dict[str, Any]] = []
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT
                id, thread_id, client_email, subject, status,
                issue_type, severity, raw_body, attachment_text,
                metadata_json, created_at, updated_at
            FROM cases
            ORDER BY created_at DESC
            """
        )
        async for row in cursor:
            rows.append(dict(row))

    return JSONResponse(content={"count": len(rows), "cases": rows})


@app.get("/cases/{case_id}", summary="Get a single case (debug)")
async def get_case(case_id: str) -> JSONResponse:
    """
    Return the full detail of one case by its UUID, including
    ``raw_body`` and ``attachment_text`` (the extracted attachment content).
    """
    async with get_db() as db:
        cursor = await db.execute(
            """
            SELECT
                id, thread_id, client_email, subject, status,
                issue_type, severity, raw_body, attachment_text,
                metadata_json, created_at, updated_at
            FROM cases WHERE id = ?
            """,
            (case_id,),
        )
        row = await cursor.fetchone()

    if row is None:
        return JSONResponse(status_code=404, content={"error": "Case not found"})

    return JSONResponse(content=dict(row))


@app.delete("/cases", summary="Delete all cases (debug)")
async def delete_all_cases() -> JSONResponse:
    """
    Truncate the ``cases`` and ``drafts`` tables.

    **Development/debug only** — wipes all data so the poller can re-ingest
    emails from scratch.  Does NOT unmark emails as unread in Gmail.
    """
    async with get_db() as db:
        await db.execute("DELETE FROM drafts")
        result = await db.execute("DELETE FROM cases")
        deleted = result.rowcount

    logger.warning("All cases deleted via DELETE /cases (%d rows removed).", deleted)
    return JSONResponse(content={"deleted": deleted, "status": "ok"})
