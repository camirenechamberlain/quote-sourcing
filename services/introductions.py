"""Recording the moment a supplier is introduced to a customer (their details are released), and what, if anything, that costs.

One Introduction per supplier per request: booking again after a change of plan never charges twice. If the customer cancels the visit (or
ends the request) before it happens, the introduction is voided, because the terms say we then waive the fee; booking again reinstates it.
Callers commit."""
from __future__ import annotations

import config
from db.models import Introduction
from services.supplier_terms import TERMS_VERSION

OPEN_STATES = ("due", "free_beta", "waived")  # not yet invoiced: these can still be voided automatically


def _cents(dollars: float) -> int:
    return int(round(dollars * 100))


def _terms_for(quote) -> tuple[str, int, str | None, object]:
    """(status, fee in cents, note, accepted_at) for a new or reinstated introduction, from whether the supplier accepted the terms."""
    accepted = getattr(quote, "fee_accepted_at", None) if quote is not None else None
    if accepted is None:
        return "waived", 0, "The supplier had not accepted the fee terms, so no fee applies.", None
    fee = _cents(config.INTRODUCTION_FEE)
    return ("due" if fee > 0 else "free_beta"), fee, None, accepted


def ensure(db, job, rfq, company, quote, trigger: str) -> Introduction:
    """Record the introduction if there isn't one yet (or reinstate a voided one). Never creates a second for the same supplier."""
    status, fee, note, accepted = _terms_for(quote)
    existing = db.query(Introduction).filter_by(rfq_send_id=rfq.id).first()
    if existing is not None:
        if existing.status == "void":
            existing.status, existing.fee_cents, existing.note, existing.accepted_at = status, fee, note, accepted
            existing.list_fee_cents = _cents(config.INTRODUCTION_FEE_LIST)
            existing.terms_version = TERMS_VERSION if accepted else None
        return existing
    intro = Introduction(job_id=job.id, rfq_send_id=rfq.id, company_id=company.id, trigger=trigger, fee_cents=fee,
                         list_fee_cents=_cents(config.INTRODUCTION_FEE_LIST), status=status, note=note, accepted_at=accepted,
                         terms_version=TERMS_VERSION if accepted else None)
    db.add(intro)
    db.flush()
    return intro


def void_for_rfq(db, rfq_send_id: str, reason: str) -> None:
    """The customer cancelled before the visit (or ended the request): waive the fee unless it has already been invoiced or paid."""
    intro = db.query(Introduction).filter_by(rfq_send_id=rfq_send_id).first()
    if intro is not None and intro.status in OPEN_STATES:
        intro.status, intro.note = "void", reason
