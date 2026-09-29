"""Email sending infrastructure via Resend API.

Provides a thin abstraction over the Resend Python SDK for sending
transactional emails (password reset, account notifications).
"""
from __future__ import annotations

import logging

import resend

from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _get_client() -> resend.Emails:
    """Return the Resend email client (initialized on first call)."""
    settings = get_settings()
    resend.api_key = settings.RESEND_API_KEY
    return resend.Emails()


def send_password_reset_email(
    to_email: str,
    reset_token: str,
    frontend_url: str,
) -> None:
    """Send a password reset email with a time-limited link.

    The link format: ``{frontend_url}/reset-password?token={reset_token}``.
    Raises on failure so the caller can log/handle it.
    """
    settings = get_settings()
    reset_url = f"{frontend_url}/reset-password?token={reset_token}"

    html_content = f"""
    <h2>Password Reset Request</h2>
    <p>You requested a password reset for your SuperTeacher account.</p>
    <p>Click the link below to set a new password. This link expires in 1 hour.</p>
    <p><a href="{reset_url}">Reset my password</a></p>
    <p>If you did not request this, you can safely ignore this email.</p>
    """

    params: resend.Emails.SendParams = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": "SuperTeacher — Reset your password",
        "html": html_content,
    }

    client = _get_client()
    result = client.send(params)
    logger.info(
        "password reset email sent",
        extra={"to": to_email, "message_id": result.get("id", "unknown")},
    )
