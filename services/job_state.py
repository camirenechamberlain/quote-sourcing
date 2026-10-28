"""What state a request is in, in words a customer understands. One place, so the dashboard, the progress page, the follow-ups and the
supplier pages all agree.

Job.status: DRAFT (entered by hand in the admin) | QUEUED (from the website, nobody contacted yet) | ACTIVE (suppliers have been emailed)
            | AWARDED (the customer chose a supplier) | CLOSED (the customer ended it).
"""
from __future__ import annotations

from dataclasses import dataclass

from db.models import Company, Job, Quote, ReplyEmail, RfqSend
from services.quote_format import quote_kind

ENDED = ("AWARDED", "CLOSED")

CLOSE_REASONS = {
    "found_elsewhere": "I found someone else",
    "not_proceeding": "I'm not going ahead",
    "no_longer_needed": "I no longer need it",
    "other": "Another reason",
}


def is_open(job: Job) -> bool:
    """A request that is still live: suppliers may respond, and follow-ups may run."""
    return job.status not in ENDED


@dataclass
class Summary:
    label: str  # "Finding suppliers", "3 quotes in", ...
    contacted: int = 0
    quotes: int = 0
    visits: int = 0
    declined: int = 0
    awaiting_shortlist: int = 0  # suppliers found and ready for the customer to approve


def shortlist(db, job: Job) -> list[Company]:
    """Suppliers waiting for the customer's say-so: found, with an email, not yet emailed, not removed by the customer."""
    sent = {r.company_id for r in db.query(RfqSend).filter(RfqSend.job_id == job.id, RfqSend.status == "sent").all()}
    return [c for c in db.query(Company).filter_by(job_id=job.id).order_by(Company.name).all()
            if c.discovered_email and c.id not in sent and (c.included or c.customer_removed)]


def summarise(db, job: Job) -> Summary:
    rfqs = db.query(RfqSend).filter(RfqSend.job_id == job.id, RfqSend.status == "sent").all()
    s = Summary(label="", contacted=len(rfqs))
    for rfq in rfqs:
        reply = db.query(ReplyEmail).filter_by(rfq_send_id=rfq.id).first()
        quote = db.query(Quote).filter_by(reply_email_id=reply.id).first() if reply else None
        kind = quote_kind(quote) if quote else None
        s.quotes += kind == "quote"
        s.visits += kind == "visit"
        s.declined += kind == "decline"
    pending = [c for c in shortlist(db, job) if c.included]
    s.awaiting_shortlist = len(pending) if not s.contacted else 0

    if job.status == "AWARDED":
        chosen = db.get(Company, job.chosen_company_id) if job.chosen_company_id else None
        s.label = f"Awarded to {chosen.name}" if chosen else "Awarded"
    elif job.status == "CLOSED":
        s.label = "Closed"
    elif s.contacted:
        bits = [f"{s.quotes} quote{'s' if s.quotes != 1 else ''}"] if s.quotes else []
        bits += [f"{s.visits} visit offer{'s' if s.visits != 1 else ''}"] if s.visits else []
        s.label = (", ".join(bits) + " in") if bits else f"Waiting on {s.contacted - s.declined} supplier{'s' if s.contacted - s.declined != 1 else ''}"
    elif s.awaiting_shortlist and job.shortlist_approved_at is None:
        s.label = "Review your supplier shortlist"
    elif job.shortlist_approved_at is not None:
        s.label = "Approved: sending to suppliers"
    else:
        s.label = "Finding suppliers"
    return s
