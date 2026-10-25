"""The one place supplier emails leave the system, so the OUTBOUND_MODE safety switch cannot be bypassed.

  dry_run (default, and what any unrecognised value means): nothing is sent; callers just show what would go out.
  test:    every email goes to TEST_RECIPIENT (your own inbox) instead of the supplier, clearly labelled, with attachments, so you
           can see exactly what a supplier would receive and reply to it to try the reply flow.
  live:    emails go to the supplier.
"""
from __future__ import annotations

import re
from typing import NamedTuple

import config

MODES = ("dry_run", "test", "live")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OutboundError(Exception):
    """The mode can't be honoured (for example test mode with no TEST_RECIPIENT). Nothing was sent."""


class Delivery(NamedTuple):
    sent: bool  # False for a dry run
    message_id: str | None
    thread_id: str | None
    sent_to: str  # who actually received it (the TEST_RECIPIENT in test mode)
    test_mode: bool


def mode() -> str:
    value = (config.OUTBOUND_MODE or "").strip().lower()
    return value if value in MODES else "dry_run"  # fail safe: an unknown or mistyped value never sends


def test_recipient() -> str:
    return (config.TEST_RECIPIENT or "").strip()


def deliver(to: str, subject: str, body: str, attachment_paths: list[str] | None = None,
            extra_files: list[tuple[str, str, bytes]] | None = None) -> Delivery:
    """Send (or not) one email according to the mode. `extra_files` are (name, mime type, bytes) built in memory (calendar
    invites). Raises OutboundError if the mode can't be honoured."""
    current = mode()
    if current == "dry_run":
        return Delivery(False, None, None, to, False)

    from services import gmail_service  # imported here so dry runs never touch Google

    extra = {"extra_files": extra_files} if extra_files else {}

    if current == "test":
        recipient = test_recipient()
        if not _EMAIL.match(recipient):
            raise OutboundError("Test mode needs TEST_RECIPIENT set to your own email address in .env. Nothing was sent.")
        banner = f"[TEST MODE: this email would have gone to {to}. Only you received it.]\n\n"
        message_id, thread_id = gmail_service.send_email(
            recipient, f"[TEST for {to}] {subject}", banner + body, attachment_paths=attachment_paths, **extra
        )
        return Delivery(True, message_id, thread_id, recipient, True)

    message_id, thread_id = gmail_service.send_email(to, subject, body, attachment_paths=attachment_paths, **extra)
    return Delivery(True, message_id, thread_id, to, False)
