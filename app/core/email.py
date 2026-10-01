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


def deliver_password_reset_email(
    to_email: str,
    reset_token: str,
    frontend_url: str,
) -> None:
    """Send a password reset email as a FastAPI ``BackgroundTasks`` job (L14).

    Scheduled by ``POST /auth/forgot-password`` only *after* the reset-token
    row has been committed, so a send failure can never leave an emailed
    token pointing at a rolled-back row — and, symmetrically, a failed send
    never rolls back a committed token.

    Never raises (the response, always 204, is already on its way) and never
    logs the token itself, only the recipient.
    """
    try:
        send_password_reset_email(
            to_email=to_email,
            reset_token=reset_token,
            frontend_url=frontend_url,
        )
    except Exception:
        logger.exception(
            "failed to send password reset email",
            extra={"to": to_email},
        )


def send_teacher_invite_email(
    to_email: str,
    invite_token: str,
    frontend_url: str,
) -> None:
    """Send a teacher invitation email with a time-limited link.

    The link format: ``{frontend_url}/accept-invite?token={invite_token}``.
    The raw token appears only in this email (and in the link the recipient
    clicks) — never in logs, API responses or database rows. Raises on
    failure so the caller can log it.
    """
    settings = get_settings()
    invite_url = f"{frontend_url}/accept-invite?token={invite_token}"

    html_content = f"""
    <h2>You're invited to SuperTeacher</h2>
    <p>An administrator created a teacher account for you.</p>
    <p>Click the link below to set your password and activate the account.
       This link expires in 72 hours.</p>
    <p><a href="{invite_url}">Accept the invitation</a></p>
    <p>If you were not expecting this, you can safely ignore this email.</p>
    """

    params: resend.Emails.SendParams = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": "SuperTeacher — Accept your teacher invitation",
        "html": html_content,
    }

    client = _get_client()
    result = client.send(params)
    logger.info(
        "teacher invitation email sent",
        extra={"to": to_email, "message_id": result.get("id", "unknown")},
    )
