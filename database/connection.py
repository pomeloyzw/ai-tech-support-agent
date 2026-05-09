"""
Database connection helper
==========================
Provides a lightweight async context-manager wrapper around aiosqlite so every
caller gets a properly configured connection without boilerplate.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    """
    Async context manager that yields a connected, row-factory-enabled
    aiosqlite connection and commits/rolls-back on exit.

    Usage::

        async with get_db() as db:
            await db.execute("SELECT 1")
    """
    conn = await aiosqlite.connect(settings.database_path)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()
