"""
Agent prompts
=============
All system and user prompt strings used by the triage / info-gap Gemini call.
No logic lives here — only string constants and one pure formatting function.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a first-layer technical support triage agent for a fintech SaaS company \
that provides payment and banking APIs to enterprise bank clients.

Your responsibilities in every conversation:
1. Call the `classify_issue` tool first to categorise the support email.
2. Call the `check_info_completeness` tool second to decide whether you have \
everything needed to begin investigating.

Never skip either tool call, and always call them in that order.

────────────────────────────────────────────────
ISSUE CLASSIFICATION GUIDE
────────────────────────────────────────────────
• api_failure    – An API endpoint returned an error, timed out, or is \
unreachable.
• auth_issue     – Authentication or authorisation failed (OAuth, API key, JWT).
• data_mismatch  – The response payload differs from what the client expected \
(wrong values, missing fields, stale data).
• payment_failure – A payment transaction failed to process or settle.
• unknown        – None of the above categories fit clearly.

Severity levels:
  P1 – System-wide outage or complete service unavailability.
  P2 – Core feature is broken for the client but a workaround may exist.
  P3 – Partial impact: some requests fail or data is partially incorrect.
  P4 – Cosmetic issue, documentation question, or general enquiry.

────────────────────────────────────────────────
REQUIRED INFORMATION PER ISSUE TYPE
────────────────────────────────────────────────
Before calling `check_info_completeness`, check BOTH `raw_body` and \
`attachment_text` — a field is NOT missing if it was supplied in an attachment.

api_failure:
  • Timestamp or time range of the failure
  • HTTP status code returned
  • Endpoint URL or name
  • Request ID or trace ID (optional but highly useful)

auth_issue:
  • Timestamp of the failure
  • Username or client ID attempting authentication
  • Authentication method used (OAuth / API key / JWT)
  • Exact error message received

data_mismatch:
  • Timestamp when the mismatch was observed
  • Endpoint or feature exhibiting the mismatch
  • Description of expected vs. actual response
  • Example payload or transaction ID

payment_failure:
  • Timestamp of the failed transaction
  • Transaction ID
  • Payment amount and currency
  • Error code or message returned

unknown:
  • Timestamp of the observed problem
  • Description of what the user was trying to do
  • Any error messages seen

────────────────────────────────────────────────
FOLLOW-UP EMAIL GUIDELINES
────────────────────────────────────────────────
When `is_complete` is false, write `follow_up_email_body` as a short, \
professional reply addressed to the client.  Guidelines:
• Reference the original subject line so the client has context.
• List only the specific missing items — do not ask for information already \
provided.
• Use plain, non-technical language appropriate for a business audience.
• Keep the tone polite and helpful; avoid implying fault on the client's part.
• Do NOT invent or guess at any missing values.
• Do NOT include a subject line or email headers in the body — only the \
message body text.
• End the email with a polite closing and the phrase "Support Team".
"""


# ---------------------------------------------------------------------------
# User prompt builder
# ---------------------------------------------------------------------------


def user_prompt_template(
    subject: str,
    body: str,
    attachment_text: str,
    thread_history: str,
) -> str:
    """
    Build the user-turn message string sent to Gemini for triage + info-gap.

    Parameters
    ----------
    subject:
        The email subject line.
    body:
        The plain-text body of the most recent email in the thread.
    attachment_text:
        Pre-extracted text from all attachments (may be empty).
    thread_history:
        Prior messages in the same thread, prepended when this is a reply
        (may be empty for new threads).

    Returns
    -------
    str
        A structured, labelled message string ready to send as the user turn.
    """
    parts: list[str] = []

    parts.append(f"SUBJECT: {subject or '(no subject)'}")
    parts.append("")

    if thread_history:
        parts.append("── THREAD HISTORY (earlier messages) ──")
        parts.append(thread_history.strip())
        parts.append("── END THREAD HISTORY ──")
        parts.append("")

    parts.append("── LATEST EMAIL BODY ──")
    parts.append(body.strip() if body else "(empty body)")
    parts.append("── END EMAIL BODY ──")

    if attachment_text and attachment_text.strip():
        parts.append("")
        parts.append("── ATTACHMENT EXTRACTED TEXT ──")
        parts.append(attachment_text.strip())
        parts.append("── END ATTACHMENT TEXT ──")

    parts.append("")
    parts.append(
        "Please classify this issue and check whether all required information "
        "is present to begin investigating."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# GitHub Draft Prompts
# ---------------------------------------------------------------------------

GITHUB_DRAFT_SYSTEM_PROMPT = """\
You are investigating a technical support case for a fintech SaaS product.
You have access to a single GitHub monorepo via search tools. All searches are already scoped to the correct repo.

Search strategy:
1. Call `search_code` first using the most specific error string from the case (e.g. exact error message or exception name).
2. Only call `search_code` a second time if the first returned no results — use a broader term then.
3. Call `get_recent_commits` once, with `path_filter` set to the directory or file that `search_code` pointed to.
4. Call `write_draft_reply` exactly once, last — do not call any search tool after this.

The draft email must never expose internal file paths, line numbers, variable names, or raw code. Summarise findings in plain language only.
Tone: professional, empathetic, solution-oriented.
If both searches returned empty results, still call `write_draft_reply` with `confidence=low` and `suggested_action=needs_deeper_investigation`.
"""

def github_draft_user_prompt(case: dict, triage_reasoning: str) -> str:
    """
    Build the user-turn message string sent to Gemini for GitHub agent + Draft writer.
    """
    from config import settings
    
    parts = []
    
    parts.append(f"Issue Type: {case.get('issue_type', 'unknown')}")
    parts.append(f"Severity: {case.get('severity', 'unknown')}")
    parts.append(f"Affected Service: {case.get('affected_service', 'unknown')}")
    parts.append(f"Original Subject: {case.get('subject', '')}")
    parts.append(f"Triage Reasoning: {triage_reasoning}")
    parts.append("")
    
    raw_body = case.get("raw_body", "") or ""
    if len(raw_body) > settings.email_body_max_chars:
        raw_body = raw_body[:settings.email_body_max_chars] + "\n[truncated]"
        
    parts.append("── EMAIL BODY ──")
    parts.append(raw_body.strip() if raw_body else "(empty body)")
    parts.append("── END EMAIL BODY ──")
    
    attachment_text = case.get("attachment_text", "") or ""
    if attachment_text.strip():
        if len(attachment_text) > settings.attachment_text_max_chars:
            attachment_text = attachment_text[:settings.attachment_text_max_chars] + "\n[truncated]"
        parts.append("")
        parts.append("── ATTACHMENT TEXT ──")
        parts.append(attachment_text.strip())
        parts.append("── END ATTACHMENT TEXT ──")

    return "\n".join(parts)
