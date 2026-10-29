"""Where requests go from first message to awarded job. The numbers that tell you whether people would pay.

A "request" is one a customer made through the website (admin-entered jobs are ignored). Test sends are left out by default: a job whose
only emails were test-mode sends never reached a supplier, so counting it would flatter the numbers."""
from __future__ import annotations

import datetime
import statistics
from dataclasses import dataclass, field

from db.models import Company, Introduction, Job, Quote, ReplyEmail, RfqSend
from services.quote_format import quote_kind


@dataclass
class Funnel:
    requests: int = 0
    with_shortlist: int = 0  # suppliers with a usable email were found
    approved: int = 0  # the customer approved the shortlist
    contacted_jobs: int = 0
    suppliers_contacted: int = 0
    suppliers_responded: int = 0
    quotes: int = 0
    visit_offers: int = 0
    declines: int = 0
    jobs_with_response: int = 0
    jobs_with_quote: int = 0
    visits_booked: int = 0
    jobs_with_visit: int = 0
    awarded: int = 0
    closed: int = 0
    avg_agreed_price: float | None = None
    median_hours_to_first_response: float | None = None
    decline_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def response_rate(self) -> float | None:
        return self.suppliers_responded / self.suppliers_contacted if self.suppliers_contacted else None

    @property
    def quotes_per_contacted_job(self) -> float | None:
        return self.quotes / self.contacted_jobs if self.contacted_jobs else None

    @property
    def award_rate(self) -> float | None:
        return self.awarded / self.contacted_jobs if self.contacted_jobs else None

    def steps(self) -> list[tuple[str, int]]:
        """Jobs reaching each stage, for a funnel chart."""
        return [
            ("Requests made", self.requests), ("Suppliers found", self.with_shortlist), ("Shortlist approved", self.approved),
            ("Suppliers contacted", self.contacted_jobs), ("Got a response", self.jobs_with_response), ("Got a quote", self.jobs_with_quote),
            ("Visit booked", self.jobs_with_visit), ("Awarded", self.awarded),
        ]


def compute(db, since: datetime.datetime | None = None, include_test: bool = False, job_ids: set[str] | None = None) -> Funnel:
    f = Funnel()
    prices: list[float] = []
    first_response_hours: list[float] = []
    query = db.query(Job).filter(Job.user_id.isnot(None))
    if since is not None:
        query = query.filter(Job.created_at >= since)
    for job in query.all():
        if job_ids is not None and job.id not in job_ids:
            continue
        rfqs = db.query(RfqSend).filter(RfqSend.job_id == job.id, RfqSend.status == "sent").all()
        is_test = bool(rfqs) and all(r.test_mode for r in rfqs)
        if is_test and not include_test:
            continue
        if not include_test:
            rfqs = [r for r in rfqs if not r.test_mode]

        f.requests += 1
        f.closed += job.status == "CLOSED"
        f.awarded += job.status == "AWARDED"
        if job.status == "AWARDED" and job.final_price:
            prices.append(job.final_price)
        if db.query(Company).filter(Company.job_id == job.id, Company.discovered_email.isnot(None)).count():
            f.with_shortlist += 1
        f.approved += job.shortlist_approved_at is not None
        if not rfqs:
            continue
        f.contacted_jobs += 1
        f.suppliers_contacted += len(rfqs)
        got_response = got_quote = False
        for rfq in rfqs:
            replies = db.query(ReplyEmail).filter_by(rfq_send_id=rfq.id).order_by(ReplyEmail.received_at).all()
            if not replies:
                continue
            f.suppliers_responded += 1
            got_response = True
            if rfq.sent_at and replies[0].received_at:
                first_response_hours.append(max(0.0, (replies[0].received_at - rfq.sent_at).total_seconds() / 3600))
            quote = db.query(Quote).filter_by(reply_email_id=replies[-1].id).first()
            kind = quote_kind(quote) if quote else "reply"
            if kind == "quote":
                f.quotes += 1
                got_quote = True
            elif kind == "visit":
                f.visit_offers += 1
            elif kind == "decline":
                f.declines += 1
                reason = quote.decline_reason or "other"
                f.decline_reasons[reason] = f.decline_reasons.get(reason, 0) + 1
        f.jobs_with_response += got_response
        f.jobs_with_quote += got_quote
        booked = db.query(Introduction).filter(Introduction.job_id == job.id, Introduction.trigger == "visit_booked", Introduction.status != "void").count()
        f.visits_booked += booked
        f.jobs_with_visit += booked > 0
    f.avg_agreed_price = sum(prices) / len(prices) if prices else None
    f.median_hours_to_first_response = statistics.median(first_response_hours) if first_response_hours else None
    return f
