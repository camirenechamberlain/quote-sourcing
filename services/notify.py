"""Send one email about a job and keep a record of it.

Everything goes through services.outbound, so the OUTBOUND_MODE switch applies (dry_run records only, test redirects to
TEST_RECIPIENT, live sends). Every email is written to the Notification table, which doubles as the dedupe memory for follow-ups
and as an audit trail. Sending never raises: a failure is recorded and the caller carries on.
"""
from __future__ import annotations

import logging

from db.models import Notification, Suppression
from services import outbound

log = logging.getLogger("notify")

_COUNTED = ("sent", "test")  # statuses that mean "this email really went out" (dry runs and failures don't count)


def is_suppressed(db, email: str | None) -> bool:
    return bool(email) and db.get(Suppression, email.strip().lower()) is not None


def suppress(db, email: str, reason: str = "opted_out") -> None:
    key = (email or "").strip().lower()
    if key and db.get(Suppression, key) is None:
        db.add(Suppression(email=key, reason=reason))


def already_sent(db, kind: str, *, rfq_send_id: str | None = None, job_id: str | None = None, audience: str | None = None) -> int:
    """How many emails of this kind have really gone out for this request (or job)."""
    q = db.query(Notification).filter(Notification.kind == kind, Notification.status.in_(_COUNTED))
    if rfq_send_id:
        q = q.filter(Notification.rfq_send_id == rfq_send_id)
    if job_id:
        q = q.filter(Notification.job_id == job_id)
    if audience:
        q = q.filter(Notification.audience == audience)
    return q.count()


def send_notification(db, *, audience: str, kind: str, to_email: str, subject: str, body: str, job_id: str | None = None,
                      rfq_send_id: str | None = None, extra_files: list[tuple[str, str, bytes]] | None = None,
                      record_dry_run: bool = True) -> Notification | None:
    """Send (per OUTBOUND_MODE) and record. Returns the Notification, or None when nothing was sent or recorded."""
    if audience == "supplier" and is_suppressed(db, to_email):
        log.info("Not emailing %s: they opted out", to_email)
        return None
    status, error = "sent", None
    try:
        delivery = outbound.deliver(to_email, subject, body, extra_files=extra_files)
        status = "dry_run" if not delivery.sent else ("test" if delivery.test_mode else "sent")
    except Exception as exc:  # noqa: BLE001 - never let a mail problem break a customer's or supplier's request
        log.exception("Could not send %s to %s", kind, to_email)
        status, error = "failed", str(exc)[:300]
    if status == "dry_run" and not record_dry_run:
        return None
    note = Notification(job_id=job_id, rfq_send_id=rfq_send_id, audience=audience, kind=kind, to_email=to_email, subject=subject,
                        body=body, status=status, error_message=error)
    db.add(note)
    db.commit()
    return note
