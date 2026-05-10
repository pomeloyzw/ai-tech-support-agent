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
from typing import Any

from google import genai
from google.genai import types

from agent.infogap import InfoGapResult, apply_infogap_result
from agent.prompts import SYSTEM_PROMPT, user_prompt_template
from agent.triage import TriageResult, apply_triage_result
from config import settings
from database.connection import get_db

logger = logging.getLogger(__name__)

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
        "model": "gemini-2.5-flash",
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
        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
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
