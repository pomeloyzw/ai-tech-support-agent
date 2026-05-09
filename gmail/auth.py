"""
Gmail OAuth2 authentication
============================
Handles the OAuth2 authorisation code flow for the Gmail API.

First run
---------
If ``GMAIL_TOKEN_PATH`` does not exist the user is directed to a local browser
window to complete the OAuth consent screen.  The resulting token is written to
``GMAIL_TOKEN_PATH`` for all future runs.

Token refresh
-------------
If the persisted token is expired and a refresh token is available it is
refreshed automatically and the updated token is saved back to disk.

Required scope
--------------
``https://www.googleapis.com/auth/gmail.modify``
(read messages + modify labels — needed to mark messages as read)
"""

from __future__ import annotations

import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.discovery import Resource

from config import settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def get_gmail_service() -> Resource:
    """
    Return an authorised Gmail API service resource.

    The function:
    1. Loads cached credentials from ``GMAIL_TOKEN_PATH`` if present.
    2. Refreshes expired credentials automatically.
    3. Runs the interactive ``InstalledAppFlow`` (opens a browser) when no
       valid credentials are found, then saves the new token to disk.

    Returns
    -------
    googleapiclient.discovery.Resource
        A ready-to-use Gmail API service object (``users()`` namespace).

    Raises
    ------
    FileNotFoundError
        If ``GMAIL_CREDENTIALS_PATH`` does not exist.
    """
    credentials: Credentials | None = None

    token_path = settings.gmail_token_path
    credentials_path = settings.gmail_credentials_path

    if not os.path.exists(credentials_path):
        raise FileNotFoundError(
            f"Gmail credentials file not found: {credentials_path!r}. "
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )

    # Load persisted token if it exists.
    if os.path.exists(token_path):
        credentials = Credentials.from_authorized_user_file(token_path, _SCOPES)
        logger.debug("Loaded Gmail credentials from %s", token_path)

    # Refresh or re-authorise as needed.
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            logger.info("Gmail token expired — refreshing …")
            credentials.refresh(Request())
            logger.info("Gmail token refreshed successfully.")
        else:
            logger.info(
                "No valid Gmail credentials found — starting OAuth2 flow. "
                "A browser window will open for authorisation."
            )
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_path, _SCOPES
            )
            credentials = flow.run_local_server(port=0)
            logger.info("OAuth2 flow completed successfully.")

        # Persist the (possibly new/refreshed) token.
        _save_token(credentials, token_path)

    return build("gmail", "v1", credentials=credentials)


def _save_token(credentials: Credentials, token_path: str) -> None:
    """Persist ``credentials`` to ``token_path`` as JSON."""
    with open(token_path, "w", encoding="utf-8") as fh:
        fh.write(credentials.to_json())
    logger.debug("Gmail token saved to %s", token_path)
