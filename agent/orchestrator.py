"""
Agent orchestrator
==================
Entry point for the triage + info-gap pipeline.  Called by the Gmail poller
after a new case or a ``pending_info`` reply is saved to the database.

Flow
----
1. Load the case from SQLite.
2. Idempotency guard — skip if status is not ``received``.
3. Build the user prompt from prior thread content + latest body.
4. Run a Gemini function-calling loop (up to ``_MAX_TURNS`` turns):
     • Turn 1 → model calls ``classify_issue``  → ``TriageResult`` captured,
                 fake result returned so the conversation continues.
     • Turn 2 → model calls ``check_info_completeness`` → ``InfoGapResult``
                 captured, fake result returned.
     • Loop ends when both results are collected or the model stops calling tools.
5. Persist results via ``apply_triage_result`` and ``apply_infogap_result``.
6. If info is incomplete, send a follow-up email to the client.
7. All exceptions are caught so the scheduler is never crashed.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from google import genai
from google.genai import types

from agent.infogap import InfoGapResult, apply_infogap_result
from agent.prompts import SYSTEM_PROMPT, user_prompt_template, GITHUB_DRAFT_SYSTEM_PROMPT, github_draft_user_prompt
from agent.triage import TriageResult, apply_triage_result
from agent.github_agent import execute_tool as execute_github_tool, GitHubFindings
from agent.draft_writer import save_draft, DraftResult
from config import settings
from database.connection import get_db

logger = logging.getLogger(__name__)

import asyncio

class GeminiRateLimiter:
    def __init__(self, rpm=14):
        self.lock = asyncio.Lock()
        self.min_interval = 60.0 / rpm
        self.last_call_time = 0.0

    async def acquire(self):
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_call_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self.last_call_time = time.monotonic()

gemini_limiter = GeminiRateLimiter(rpm=14)

# Maximum function-calling turns before giving up (guards against infinite loops).
_MAX_TURNS = 6

# ---------------------------------------------------------------------------
# Gemini tool definitions
# ---------------------------------------------------------------------------

_GEMINI_TOOLS = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="classify_issue",
            description=(
                "Classify the support issue based on the email content. "
                "Call this first for every case."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "issue_type": types.Schema(
                        type=types.Type.STRING,
                        enum=[
                            "api_failure",
                            "auth_issue",
                            "data_mismatch",
                            "payment_failure",
                            "unknown",
                        ],
                        description="The primary category of the issue.",
                    ),
                    "severity": types.Schema(
                        type=types.Type.STRING,
                        enum=["P1", "P2", "P3", "P4"],
                        description=(
                            "P1=system down, P2=core feature broken, "
                            "P3=partial impact, P4=cosmetic or question"
                        ),
                    ),
                    "affected_service": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "The specific API endpoint, service name, or feature mentioned. "
                            "Use 'unknown' if not mentioned."
                        ),
                    ),
                    "confidence": types.Schema(
                        type=types.Type.NUMBER,
                        description="0.0–1.0. How confident are you in this classification.",
                    ),
                    "reasoning": types.Schema(
                        type=types.Type.STRING,
                        description="One sentence explaining why you chose this classification.",
                    ),
                },
                required=[
                    "issue_type",
                    "severity",
                    "affected_service",
                    "confidence",
                    "reasoning",
                ],
            ),
        ),
        types.FunctionDeclaration(
            name="check_info_completeness",
            description=(
                "After classifying the issue, check whether the email contains all the "
                "information needed to investigate. Call this second, after classify_issue."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "is_complete": types.Schema(
                        type=types.Type.BOOLEAN,
                        description="True if all required fields are present, false otherwise.",
                    ),
                    "missing_fields": types.Schema(
                        type=types.Type.ARRAY,
                        items=types.Schema(type=types.Type.STRING),
                        description=(
                            "List of specific missing pieces of information. "
                            "Empty array if is_complete is true."
                        ),
                    ),
                    "follow_up_email_body": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "If is_complete is false: a polite, professional follow-up email body "
                            "addressed to the client asking for exactly the missing fields. "
                            "Mention the original subject so context is clear. "
                            "If is_complete is true: empty string."
                        ),
                    ),
                },
                required=["is_complete", "missing_fields", "follow_up_email_body"],
            ),
        ),
    ]
)

_GITHUB_DRAFT_TOOLS = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="search_code",
            description=(
                "Search the codebase for source code relevant to the issue. "
                "Use specific terms: exact error message strings, exception class names, "
                "endpoint paths, or function names. "
                "You may call this tool at most TWICE — make each query count. "
                "First call: use the most specific error string from the issue. "
                "Second call (only if needed): broaden to endpoint name or feature area."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "query": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Exact search term. Prefer specific strings over general ones. "
                            "Good: 'PAYMENT_GATEWAY_TIMEOUT' or 'INSUFFICIENT_FUNDS'. "
                            "Bad: 'payment error' or 'transfer problem'."
                        )
                    ),
                    "file_extension": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Filter by file type. Use 'py' by default. "
                            "Only change if the issue clearly involves a different language."
                        )
                    )
                },
                required=["query"]
            )
        ),
        types.FunctionDeclaration(
            name="get_recent_commits",
            description=(
                "Fetch recent commits from the last 7 days to check if a code change "
                "may have introduced the issue. "
                "Call this once. Use path_filter to narrow to the relevant area "
                "based on what search_code found — e.g. 'payments/' or 'auth/'. "
                "Only skip path_filter if you have no idea which area is affected."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "path_filter": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Limit commits to those touching files under this path. "
                            "E.g. 'payments/', 'api/accounts.py', 'auth/'. "
                            "Always provide this when possible — omitting it returns "
                            "all commits which is noisy and token-heavy."
                        )
                    )
                },
                required=[]
            )
        ),
        types.FunctionDeclaration(
            name="write_draft_reply",
            description=(
                "Write the final draft reply email to the client. "
                "Call this exactly once, after search_code and get_recent_commits. "
                "Do not call more search tools after this. "
                "If findings are limited, still write the draft — acknowledge the issue "
                "and state that investigation is ongoing."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "root_cause_summary": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "One or two sentences on the likely root cause. "
                            "If unknown: 'Root cause is under investigation.'"
                        )
                    ),
                    "draft_email_body": types.Schema(
                        type=types.Type.STRING,
                        description=(
                            "Full email body to send to the client. "
                            "Professional, concise, no jargon. Must include: "
                            "1) Acknowledgement of the issue "
                            "2) Summary of findings in plain language "
                            "3) Next steps or resolution timeline. "
                            "Do NOT include file paths, line numbers, or raw code."
                        )
                    ),
                    "confidence": types.Schema(
                        type=types.Type.STRING,
                        enum=["high", "medium", "low"],
                        description=(
                            "high=clear bug found, medium=suspicious code/commit found, "
                            "low=no clear finding."
                        )
                    ),
                    "suggested_action": types.Schema(
                        type=types.Type.STRING,
                        enum=["bug_confirmed", "needs_deeper_investigation", "user_error", "known_issue"],
                        description="Recommended next action for the IT engineer."
                    )
                },
                required=["root_cause_summary", "draft_email_body", "confidence", "suggested_action"]
            )
        )
    ]
)

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def process_case(case_id: str) -> None:
    """
    Run the triage + info-gap pipeline for one case.

    Parameters
    ----------
    case_id:
        UUID of the case to process.  The case must already exist in the
        ``cases`` table with ``status = 'received'``.

    Notes
    -----
    This function is designed to be called as a fire-and-forget background
    task from the Gmail poller.  It catches all exceptions internally so
    that a failure never propagates to and crashes the APScheduler job.
    """
    try:
        await _run_pipeline(case_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Unhandled exception in process_case(%s) — case status unchanged: %s",
            case_id,
            exc,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Internal pipeline
# ---------------------------------------------------------------------------


async def _run_pipeline(case_id: str) -> None:
    """Execute the full triage → info-gap flow for ``case_id``."""
    async with get_db() as db:
        case = await _load_case(db, case_id)

    if case is None:
        logger.warning("process_case: case %s not found in DB — skipping.", case_id)
        return

    # Idempotency guard: only process cases that are freshly received.
    if case["status"] != "received":
        logger.debug(
            "process_case: skipping case %s (status=%s, expected 'received').",
            case_id,
            case["status"],
        )
        return

    subject: str = case["subject"] or "(no subject)"
    raw_body: str = case["raw_body"] or ""
    attachment_text: str = case["attachment_text"] or ""
    thread_id: str = case["thread_id"]
    client_email: str = case["client_email"]

    # Build the user message. Thread history is already embedded in raw_body
    # (the poller appends replies with a separator).
    user_message = user_prompt_template(
        subject=subject,
        body=raw_body,
        attachment_text=attachment_text,
        thread_history="",
    )

    logger.info("Calling Gemini for case %s (thread=%s) …", case_id, thread_id)

    input_payload = {
        "model": "gemini-3.1-flash-lite-preview",
        "case_id": case_id,
        "subject": subject,
        "body_length": len(raw_body),
        "attachment_text_length": len(attachment_text),
    }
    input_json = json.dumps(input_payload, ensure_ascii=False)

    triage_result, infogap_result, raw_calls, duration_ms = await _call_gemini(
        user_message
    )

    if triage_result is None or infogap_result is None:
        logger.error(
            "Gemini did not return both tool calls for case %s — "
            "triage=%s infogap=%s.  Case status unchanged.",
            case_id,
            triage_result,
            infogap_result,
        )
        return

    tool_calls_json = _serialise_tool_calls(raw_calls)
    output_json = json.dumps(
        {
            "triage": {
                "issue_type": triage_result.issue_type,
                "severity": triage_result.severity,
                "affected_service": triage_result.affected_service,
                "confidence": triage_result.confidence,
                "reasoning": triage_result.reasoning,
            },
            "infogap": {
                "is_complete": infogap_result.is_complete,
                "missing_fields": infogap_result.missing_fields,
            },
        },
        ensure_ascii=False,
    )

    async with get_db() as db:
        await apply_triage_result(
            case_id,
            triage_result,
            db,
            input_json=input_json,
            output_json=output_json,
            tool_calls_json=tool_calls_json,
            duration_ms=duration_ms,
        )
        await apply_infogap_result(
            case_id,
            infogap_result,
            db,
            input_json=input_json,
            output_json=output_json,
            tool_calls_json=tool_calls_json,
            duration_ms=duration_ms,
        )

    if not infogap_result.is_complete and infogap_result.follow_up_email_body:
        await _send_follow_up(
            thread_id=thread_id,
            client_email=client_email,
            subject=subject,
            body=infogap_result.follow_up_email_body,
        )
    elif infogap_result.is_complete:
        async with get_db() as github_db:
            await run_github_and_draft(case_id, github_db, triage_result.reasoning)


async def run_github_and_draft(case_id: str, db: Any, triage_reasoning: str) -> None:
    """Execute Step 5: GitHub code search, commit search, and draft generation."""
    case = await _load_case(db, case_id)
    if not case:
        logger.warning("run_github_and_draft: case %s not found in DB.", case_id)
        return

    logger.info("Running GitHub agent + draft writer for case %s ...", case_id)
    user_prompt = github_draft_user_prompt(case, triage_reasoning)
    
    client = genai.Client(api_key=settings.gemini_api_key)
    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[types.Part(text=user_prompt)],
        )
    ]
    
    findings = GitHubFindings(
        code_results=[],
        commit_results=[],
        search_queries_used=[],
        total_duration_ms=0,
    )
    draft_result: DraftResult | None = None
    all_tool_calls = []
    
    start = time.monotonic()
    
    # Cap loop at 5 iterations
    for turn in range(5):
        try:
            await gemini_limiter.acquire()
            response = await client.aio.models.generate_content(
                model="gemini-3.1-flash-lite-preview",
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=GITHUB_DRAFT_SYSTEM_PROMPT,
                    tools=[_GITHUB_DRAFT_TOOLS],
                    tool_config=types.ToolConfig(
                        function_calling_config=types.FunctionCallingConfig(
                            mode="AUTO",
                        )
                    ),
                    max_output_tokens=1500,
                ),
            )
        except Exception as e:
            logger.error("Gemini API error in run_github_and_draft: %s", e)
            break
            
        if not response.candidates:
            break
            
        candidate = response.candidates[0]
        if not candidate.content.parts:
            break
            
        contents.append(candidate.content)
        
        # Check for tool calls
        function_response_parts: list[types.Part] = []
        has_tool_call = False
        
        for part in candidate.content.parts:
            if part.function_call is None:
                continue
                
            has_tool_call = True
            fc = part.function_call
            args = dict(fc.args)
            
            all_tool_calls.append({"name": fc.name, "args": args})
            logger.debug("GitHub agent tool call (turn %d): %s(%s)", turn + 1, fc.name, args)
            
            if fc.name == "search_code":
                findings.search_queries_used.append(args.get("query", ""))
            
            # Execute tool and collect result
            try:
                result_dict = await execute_github_tool(fc.name, args)
            except Exception as e:
                logger.error("Error executing tool %s: %s", fc.name, e)
                result_dict = {"error": str(e)}
                
            if fc.name == "search_code" and "code_results" in result_dict:
                from agent.github_agent import CodeSearchResult
                findings.code_results.extend(
                    [CodeSearchResult(**r) for r in result_dict["code_results"]]
                )
            elif fc.name == "get_recent_commits" and "commit_results" in result_dict:
                from agent.github_agent import CommitResult
                findings.commit_results.extend(
                    [CommitResult(**r) for r in result_dict["commit_results"]]
                )
            elif fc.name == "write_draft_reply":
                draft_result = DraftResult(
                    root_cause_summary=args.get("root_cause_summary", ""),
                    draft_email_body=args.get("draft_email_body", ""),
                    confidence=args.get("confidence", "low"),
                    suggested_action=args.get("suggested_action", "needs_deeper_investigation"),
                )
                
            function_response_parts.append(
                types.Part.from_function_response(
                    name=fc.name,
                    response=result_dict,
                )
            )
            
        if function_response_parts:
            contents.append(
                types.Content(role="user", parts=function_response_parts)
            )
            
        if draft_result:
            logger.info("Draft generated for case %s after %d turn(s).", case_id, turn + 1)
            break
            
        if not has_tool_call:
            logger.warning("GitHub agent stopped calling tools before draft was written.")
            break
    else:
        logger.warning("GitHub agent loop capped at 5 iterations for case %s.", case_id)
        
    findings.total_duration_ms = int((time.monotonic() - start) * 1000)
    
    if draft_result:
        try:
            await save_draft(case_id, draft_result, findings, db)
            
            # Log to agent_logs
            log_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            await db.execute(
                """
                INSERT INTO agent_logs (id, case_id, step, input_json, output_json, tool_calls_json, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    log_id,
                    case_id,
                    "github_draft",
                    json.dumps({"user_prompt_length": len(user_prompt)}),
                    json.dumps(vars(draft_result), ensure_ascii=False),
                    json.dumps(all_tool_calls, ensure_ascii=False),
                    findings.total_duration_ms,
                    now
                )
            )
        except Exception as e:
            logger.error("Failed to save draft for case %s: %s", case_id, e)


# ---------------------------------------------------------------------------
# Gemini API call  (function-calling loop)
# ---------------------------------------------------------------------------


async def _call_gemini(
    user_message: str,
) -> tuple[TriageResult | None, InfoGapResult | None, list[Any], int]:
    """
    Drive a Gemini function-calling conversation until both tools have been
    called or the model stops requesting functions.

    Gemini calls one tool at a time.  After each ``function_call`` part we
    send back a lightweight ``function_response`` so the model continues.
    We capture the arguments from each call without actually executing
    anything — the Python apply functions handle the real side-effects later.

    Returns
    -------
    tuple of:
        - ``TriageResult | None``
        - ``InfoGapResult | None``
        - list of raw function-call dicts (for observability logging)
        - total elapsed milliseconds
    """
    client = genai.Client(api_key=settings.gemini_api_key)

    # Conversation history: starts with the user message.
    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[types.Part(text=user_message)],
        )
    ]

    triage_result: TriageResult | None = None
    infogap_result: InfoGapResult | None = None
    all_function_calls: list[dict[str, Any]] = []

    start = time.monotonic()

    for turn in range(_MAX_TURNS):
        await gemini_limiter.acquire()
        response = await client.aio.models.generate_content(
            model="gemini-3.1-flash-lite-preview",
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=[_GEMINI_TOOLS],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode="AUTO",
                    )
                ),
            ),
        )

        # Append the model's reply to the conversation history.
        candidate = response.candidates[0]
        contents.append(candidate.content)

        # Collect function calls from this turn and build response parts.
        function_response_parts: list[types.Part] = []
        has_function_call = False

        for part in candidate.content.parts:
            if part.function_call is None:
                continue

            has_function_call = True
            fc = part.function_call
            args: dict[str, Any] = dict(fc.args)

            all_function_calls.append({"name": fc.name, "args": args})
            logger.debug("Gemini function call (turn %d): %s(%s)", turn + 1, fc.name, args)

            if fc.name == "classify_issue":
                triage_result = TriageResult(
                    issue_type=args["issue_type"],
                    severity=args["severity"],
                    affected_service=args.get("affected_service", "unknown"),
                    confidence=float(args.get("confidence", 0.0)),
                    reasoning=args.get("reasoning", ""),
                )
                logger.debug(
                    "classify_issue → type=%s severity=%s confidence=%.2f",
                    triage_result.issue_type,
                    triage_result.severity,
                    triage_result.confidence,
                )
                function_response_parts.append(
                    types.Part.from_function_response(
                        name="classify_issue",
                        response={"result": "Classification recorded."},
                    )
                )

            elif fc.name == "check_info_completeness":
                infogap_result = InfoGapResult(
                    is_complete=bool(args["is_complete"]),
                    missing_fields=list(args.get("missing_fields", [])),
                    follow_up_email_body=args.get("follow_up_email_body", ""),
                )
                logger.debug(
                    "check_info_completeness → complete=%s missing=%s",
                    infogap_result.is_complete,
                    infogap_result.missing_fields,
                )
                function_response_parts.append(
                    types.Part.from_function_response(
                        name="check_info_completeness",
                        response={"result": "Completeness check recorded."},
                    )
                )

        # If we got function calls, push the results back and continue.
        if function_response_parts:
            contents.append(
                types.Content(role="user", parts=function_response_parts)
            )

        # Stop when both results are in hand or the model has nothing more to call.
        if triage_result and infogap_result:
            logger.debug("Both tools completed after %d turn(s).", turn + 1)
            break

        if not has_function_call:
            logger.warning(
                "Gemini stopped calling tools after %d turn(s) — "
                "triage=%s infogap=%s.",
                turn + 1,
                triage_result,
                infogap_result,
            )
            break

    duration_ms = int((time.monotonic() - start) * 1000)
    return triage_result, infogap_result, all_function_calls, duration_ms


# ---------------------------------------------------------------------------
# Follow-up email dispatch
# ---------------------------------------------------------------------------


async def _send_follow_up(
    thread_id: str,
    client_email: str,
    subject: str,
    body: str,
) -> None:
    """
    Send a follow-up email to the client via the Gmail sender module.

    Failures are logged but never propagated — the case status has already
    been set to ``pending_info`` in the database.
    """
    try:
        from gmail.auth import get_gmail_service
        from gmail.sender import send_reply

        gmail_service = get_gmail_service()
        await send_reply(
            thread_id=thread_id,
            to_email=client_email,
            subject=subject,
            body=body,
            gmail_service=gmail_service,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to send follow-up email for thread %s to %s: %s",
            thread_id,
            client_email,
            exc,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


async def _load_case(db: Any, case_id: str) -> dict[str, Any] | None:
    """Fetch a single case row as a dict, or ``None`` if not found."""
    cursor = await db.execute(
        """
        SELECT id, thread_id, client_email, subject, status,
               raw_body, attachment_text, metadata_json
        FROM cases WHERE id = ?
        """,
        (case_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    keys = [
        "id", "thread_id", "client_email", "subject", "status",
        "raw_body", "attachment_text", "metadata_json",
    ]
    return dict(zip(keys, row))


def _serialise_tool_calls(function_calls: list[dict[str, Any]]) -> str:
    """Serialise the collected function-call dicts to a JSON string for logging."""
    try:
        return json.dumps(function_calls, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return "[]"
