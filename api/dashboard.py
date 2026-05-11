from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from database.connection import get_db
from gmail.auth import get_gmail_service
from gmail.sender import send_reply

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["dashboard"])

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class DraftPatchRequest(BaseModel):
    draft_body: str

class EscalateRequest(BaseModel):
    note: Optional[str] = None

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/cases")
async def get_cases(
    status: Optional[str] = "draft_ready",
    limit: int = Query(20, le=100),
    offset: int = 0
) -> dict[str, Any]:
    """
    Get a paginated list of cases.
    Filters by status unless status='all'.
    Includes confidence and suggested_action from the draft if available.
    """
    where_clause = ""
    params: list[Any] = []
    
    if status and status.lower() != "all":
        where_clause = "WHERE c.status = ?"
        params.append(status)
        
    query_count = f"SELECT COUNT(*) FROM cases c {where_clause}"
    query_cases = f"""
        SELECT 
            c.id, c.subject, c.client_email, c.issue_type, c.severity, 
            c.status, c.created_at, c.updated_at,
            d.evidence_json
        FROM cases c
        LEFT JOIN drafts d ON c.id = d.case_id
        {where_clause}
        ORDER BY c.created_at DESC
        LIMIT ? OFFSET ?
    """
    
    async with get_db() as db:
        cursor = await db.execute(query_count, params)
        total_row = await cursor.fetchone()
        total = total_row[0] if total_row else 0
        
        cursor = await db.execute(query_cases, params + [limit, offset])
        rows = await cursor.fetchall()
        
    cases_list = []
    for row in rows:
        case_dict = {
            "id": row["id"],
            "subject": row["subject"],
            "client_email": row["client_email"],
            "issue_type": row["issue_type"],
            "severity": row["severity"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "confidence": None,
            "suggested_action": None
        }
        if row["evidence_json"]:
            try:
                evidence = json.loads(row["evidence_json"])
                case_dict["confidence"] = evidence.get("confidence")
                case_dict["suggested_action"] = evidence.get("suggested_action")
            except json.JSONDecodeError:
                pass
                
        cases_list.append(case_dict)
        
    return {"total": total, "cases": cases_list}

@router.get("/cases/{case_id}")
async def get_case_detail(case_id: str) -> dict[str, Any]:
    """
    Get full case details, including draft and agent logs.
    """
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM cases WHERE id = ?", (case_id,))
        case_row = await cursor.fetchone()
        
        if not case_row:
            raise HTTPException(status_code=404, detail="Case not found")
            
        case_dict = dict(case_row)
        if case_dict.get("metadata_json"):
            try:
                case_dict["metadata_json"] = json.loads(case_dict["metadata_json"])
            except json.JSONDecodeError:
                pass
                
        # Get draft
        cursor = await db.execute("SELECT * FROM drafts WHERE case_id = ?", (case_id,))
        draft_row = await cursor.fetchone()
        
        if draft_row:
            draft_dict = dict(draft_row)
            if draft_dict.get("evidence_json"):
                try:
                    draft_dict["evidence"] = json.loads(draft_dict["evidence_json"])
                except json.JSONDecodeError:
                    draft_dict["evidence"] = {}
                del draft_dict["evidence_json"]
            case_dict["draft"] = draft_dict
        else:
            case_dict["draft"] = None
            
        # Get logs (excluding large json columns)
        cursor = await db.execute(
            """
            SELECT step, duration_ms, created_at 
            FROM agent_logs 
            WHERE case_id = ? 
            ORDER BY created_at ASC
            """, 
            (case_id,)
        )
        logs = await cursor.fetchall()
        case_dict["agent_logs"] = [dict(log) for log in logs]
        
    return case_dict

@router.patch("/cases/{case_id}/draft")
async def update_draft(case_id: str, payload: DraftPatchRequest) -> dict[str, Any]:
    """
    Update the draft body for a case.
    """
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM drafts WHERE case_id = ?", (case_id,))
        draft_row = await cursor.fetchone()
        
        if not draft_row:
            raise HTTPException(status_code=404, detail="Draft not found")
            
        await db.execute(
            "UPDATE drafts SET draft_body = ? WHERE case_id = ?",
            (payload.draft_body, case_id)
        )
        await db.commit()
        
    return {"ok": True}

@router.post("/cases/{case_id}/approve")
async def approve_draft(case_id: str) -> dict[str, Any]:
    """
    Approve and send the draft reply via Gmail.
    """
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT thread_id, client_email, subject FROM cases WHERE id = ?", 
            (case_id,)
        )
        case_row = await cursor.fetchone()
        if not case_row:
            raise HTTPException(status_code=404, detail="Case not found")
            
        cursor = await db.execute(
            "SELECT id, draft_body FROM drafts WHERE case_id = ?", 
            (case_id,)
        )
        draft_row = await cursor.fetchone()
        if not draft_row:
            raise HTTPException(status_code=404, detail="Draft not found")
            
    try:
        gmail_svc = get_gmail_service()
        await send_reply(
            thread_id=case_row["thread_id"],
            to_email=case_row["client_email"],
            subject=case_row["subject"] or "Technical Support",
            body=draft_row["draft_body"] or "",
            gmail_service=gmail_svc
        )
    except Exception as e:
        logger.error(f"Failed to send email for case {case_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send: {e}"
        )
        
    now_iso = datetime.now().isoformat()
    async with get_db() as db:
        await db.execute(
            """
            UPDATE drafts 
            SET approved_by = 'engineer', approved_at = ?, sent_at = ? 
            WHERE case_id = ?
            """,
            (now_iso, now_iso, case_id)
        )
        await db.execute(
            "UPDATE cases SET status = 'sent', updated_at = ? WHERE id = ?",
            (now_iso, case_id)
        )
        await db.commit()
        
    return {"ok": True, "sent_at": now_iso}

@router.post("/cases/{case_id}/escalate")
async def escalate_case(case_id: str, payload: EscalateRequest) -> dict[str, Any]:
    """
    Mark a case as escalated.
    """
    now_iso = datetime.now().isoformat()
    async with get_db() as db:
        cursor = await db.execute("SELECT id FROM cases WHERE id = ?", (case_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Case not found")
            
        await db.execute(
            """
            UPDATE cases 
            SET status = 'escalated', updated_at = ?, escalation_note = ? 
            WHERE id = ?
            """,
            (now_iso, payload.note, case_id)
        )
        await db.commit()
        
    return {"ok": True}

@router.get("/stats")
async def get_stats() -> dict[str, Any]:
    """
    Get case counts for the dashboard header.
    """
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    
    stats = {
        "draft_ready": 0,
        "pending_info": 0,
        "investigating": 0,
        "sent_today": 0,
        "escalated": 0
    }
    
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT status, COUNT(*) as count FROM cases GROUP BY status"
        )
        rows = await cursor.fetchall()
        for row in rows:
            if row["status"] in stats:
                stats[row["status"]] = row["count"]
                
        # Get sent_today
        cursor = await db.execute(
            """
            SELECT COUNT(*) FROM cases c
            JOIN drafts d ON c.id = d.case_id
            WHERE c.status = 'sent' AND d.sent_at >= ?
            """,
            (today_start,)
        )
        sent_row = await cursor.fetchone()
        if sent_row:
            stats["sent_today"] = sent_row[0]
            
    return stats
