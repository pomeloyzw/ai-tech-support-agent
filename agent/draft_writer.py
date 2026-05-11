"""
Draft writer
============
Saves the draft email and GitHub findings into the drafts table.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from agent.github_agent import GitHubFindings

logger = logging.getLogger(__name__)


@dataclass
class DraftResult:
    root_cause_summary: str
    draft_email_body: str
    confidence: str
    suggested_action: str


async def save_draft(
    case_id: str, 
    result: DraftResult, 
    findings: GitHubFindings, 
    db: Any
) -> None:
    """
    Save the generated draft to the database and update the case status.
    """
    logger.info("Saving draft for case %s (confidence: %s)", case_id, result.confidence)
    
    evidence_dict = {
        "root_cause_summary": result.root_cause_summary,
        "confidence": result.confidence,
        "suggested_action": result.suggested_action,
        "github_code_results": [
            {
                "file_path": cr.file_path,
                "url": cr.html_url,
                "matched_lines": cr.matched_lines,
            }
            for cr in findings.code_results
        ],
        "github_commits": [
            {
                "sha": c.sha,
                "message": c.message,
                "author": c.author,
                "url": c.html_url,
            }
            for c in findings.commit_results
        ],
        "search_queries": findings.search_queries_used,
    }
    
    evidence_json = json.dumps(evidence_dict, ensure_ascii=False)
    
    now = datetime.now(timezone.utc).isoformat()
    draft_id = str(uuid.uuid4())
    
    # Insert into drafts table
    await db.execute(
        """
        INSERT INTO drafts (id, case_id, draft_body, evidence_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (draft_id, case_id, result.draft_email_body, evidence_json, now)
    )
    
    # Update cases table
    await db.execute(
        """
        UPDATE cases
        SET status = 'draft_ready', updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (case_id,)
    )
