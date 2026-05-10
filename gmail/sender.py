"""
Gmail reply sender
==================
Sends an RFC 2822-compliant email reply via the Gmail API, placing it in the
correct Gmail thread by setting the ``In-Reply-To`` and ``References`` headers.

The blocking Gmail API call is offloaded to ``asyncio.run_in_executor`` so the
event loop is never blocked, matching the pattern used in
``preprocessor/attachments.py``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any

logger = logging.getLogger(__name__)


async def send_reply(
    thread_id: str,
    to_email: str,
    subject: str,
    body: str,
    gmail_service: Any,
) -> None:
    """
    Send a plain-text reply email that appears inside an existing Gmail thread.

    Parameters
    ----------
    thread_id:
        The Gmail thread ID.  Used both as the ``threadId`` in the send
        request (so Gmail places the message in the right thread) and as
        the ``In-Reply-To`` / ``References`` header value.
    to_email:
        Recipient email address.
    subject:
        Original subject line.  ``Re: `` is prepended automatically if the
        subject does not already start with that prefix (case-insensitive).
    body:
        Plain-text body of the reply.
    gmail_service:
        An authenticated ``googleapiclient.discovery.Resource`` object for
        the Gmail API (returned by ``gmail.auth.get_gmail_service``).

    Raises
    ------
    googleapiclient.errors.HttpError
        Propagated on a non-retried API failure so the caller can log it.
    """
    reply_subject = (
        subject
        if subject.lower().startswith("re:")
        else f"Re: {subject}"
    )

    message = MIMEText(body, "plain", "utf-8")
    message["To"] = to_email
    message["Subject"] = reply_subject
    message["Date"] = formatdate(localtime=True)
    # These headers ensure Gmail threads the reply correctly.
    message["In-Reply-To"] = thread_id
    message["References"] = thread_id

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    send_body = {"raw": raw, "threadId": thread_id}

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: (
            gmail_service.users()
            .messages()
            .send(userId="me", body=send_body)
            .execute()
        ),
    )

    logger.info(
        "Follow-up reply sent to %s (thread=%s, subject=%r).",
        to_email,
        thread_id,
        reply_subject,
    )
