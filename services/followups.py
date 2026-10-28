"""Automated follow-ups: the nudges that keep a quote request moving without anyone chasing by hand.

Rules (all emails go through services.notify, so OUTBOUND_MODE, supplier opt-outs and the audit trail apply):
  chase_1 / chase_2      supplier hasn't responded: nudge at 3 days and 7 days (never more, never after 14 days)
  customer_digest        one email (at most every 4 hours) summarising new quotes, visit offers and declines
  pick_reminder_1 / _2   customer hasn't chosen a visit time: remind after 24 hours and 72 hours
  visit_reminder_*       both sides, within 24 hours before a confirmed visit
  post_visit_1 / _2      supplier visited but hasn't sent a quote: nudge 2 and 6 days after the visit
  quotes_waiting         customer has quotes and hasn't looked: one nudge after 3 days

Only sends during local business hours (Mon to Sat), at most MAX_PER_RUN emails per run and DAILY_CAP per day. Each rule remembers what it
has sent in the Notification table, so running it as often as you like never double-sends.

Run it from the command line (`python -m services.followups`, add --send to really send) or let the website run it on a timer
(FOLLOWUPS_ENABLED=1, see server/main.py).
"""
from __future__ import annotations

import argparse
import datetime
import logging
from dataclasses import dataclass, field
from typing import Callable

import config
from db.models import Company, Job, Notification, Quote, ReplyEmail, RfqSend, User, Visit
from db.session import get_session
from services import outbound, timeutil
from services.notify import already_sent, is_suppressed, send_notification
from services.quote_format import describe_price, quote_kind, window_labels  # noqa: F401 (quote_kind re-exported for tests)

log = logging.getLogger("followups")

DAY = datetime.timedelta(days=1)
CHASE_1_AFTER, CHASE_2_AFTER, CHASE_GAP, STOP_CHASING_AFTER = 3 * DAY, 7 * DAY, 2 * DAY, 14 * DAY
DIGEST_EVERY = datetime.timedelta(hours=4)
PICK_REMINDER_AFTER = (1 * DAY, 3 * DAY)
VISIT_REMINDER_WINDOW = datetime.timedelta(hours=24)
POST_VISIT_AFTER = (2 * DAY, 6 * DAY)
STOP_POST_VISIT_AFTER = 13 * DAY
QUOTES_WAITING_AFTER = 3 * DAY
MAX_PER_RUN = 40
DAILY_CAP = 200
FIRST_HOUR, LAST_HOUR = 8, 18  # local time; nothing is sent outside these hours or on Sundays


@dataclass
class Planned:
    audience: str
    kind: str
    to_email: str
    subject: str
    body: str
    job_id: str
    rfq_send_id: str | None = None
    on_sent: Callable[[], None] | None = field(default=None, repr=False)  # e.g. mark responses as told to the customer
    needs_link: bool = False  # the email is pointless without its link, so it is held back until PUBLIC_BASE_URL is set


@dataclass
class Report:
    planned: list[Planned]
    sent: int = 0
    note: str = ""


@dataclass
class Ctx:
    db: object
    now: datetime.datetime  # naive UTC, like the database
    base_url: str
    mode: str

    @property
    def local(self) -> datetime.datetime:
        return timeutil.utc_to_local(self.now)


def in_business_hours(local: datetime.datetime) -> bool:
    return FIRST_HOUR <= local.hour < LAST_HOUR and local.weekday() != 6


def _link(ctx: Ctx, path: str) -> str:
    return f"{ctx.base_url}{path}" if ctx.base_url else ""


def _customer(db, job: Job) -> User | None:
    return db.get(User, job.user_id) if job.user_id else None


def _reachable(rfq: RfqSend, ctx: Ctx) -> bool:
    """A test send never reached the supplier, so never chase them for real; in test mode everything is redirected to you anyway."""
    return not (rfq.test_mode and ctx.mode == "live")


def _sign(customer: User | None) -> str:
    return f"Quoteschmote, on behalf of {customer.name}" if customer and customer.name else "Quoteschmote"


def _last_sent_at(db, kind: str, rfq_send_id: str) -> datetime.datetime | None:
    row = (db.query(Notification).filter(Notification.kind == kind, Notification.rfq_send_id == rfq_send_id, Notification.status.in_(("sent", "test")))
           .order_by(Notification.created_at.desc()).first())
    return row.created_at if row else None


# ---- supplier chases ---------------------------------------------------------------------------------------------
def _chases(ctx: Ctx):
    db = ctx.db
    for rfq in db.query(RfqSend).filter(RfqSend.status == "sent", RfqSend.sent_at.isnot(None)).all():
        age = ctx.now - rfq.sent_at
        if age < CHASE_1_AFTER or age > STOP_CHASING_AFTER or not _reachable(rfq, ctx):
            continue
        if db.query(ReplyEmail).filter_by(rfq_send_id=rfq.id).count():  # they answered (by email or on the page)
            continue
        first = already_sent(db, "chase_1", rfq_send_id=rfq.id)
        if not first:
            kind = "chase_1"
        elif not already_sent(db, "chase_2", rfq_send_id=rfq.id) and age >= CHASE_2_AFTER:
            last = _last_sent_at(db, "chase_1", rfq.id)
            if last and ctx.now - last < CHASE_GAP:
                continue
            kind = "chase_2"
        else:
            continue
        job, company = db.get(Job, rfq.job_id), db.get(Company, rfq.company_id)
        customer = _customer(db, job)
        url = _link(ctx, f"/q/{rfq.response_token}") if rfq.response_token else ""
        how = (f"The quickest way to answer is here (about a minute, no sign-up):\n{url}\n\nIf it's not for you, there's a button for that "
               f"too, so we know not to ask again.\n" if url else "Just reply to this email with your price, or let us know if it's not for you.\n")
        opener = ("A quick follow-up in case my earlier email got buried" if kind == "chase_1" else "One last nudge, then I'll leave you be")
        who = customer.name if customer and customer.name else "the customer"
        opt_out = f"\nNot interested in requests like this? {url}/optout\n" if url else ""
        body = (f"Hi {company.name} team,\n\n{opener}: {who} is still looking for a quote for the {job.category} job in "
                f"{job.location_text} (ref {job.ref_prefix}).\n\n{how}\nThanks,\n{_sign(customer)}\n{opt_out}")
        yield Planned("supplier", kind, rfq.recipient_email, f"Following up: {rfq.subject}"[:200], body, job.id, rfq.id)


# ---- customer digest -----------------------------------------------------------------------------------------------
def _digest(ctx: Ctx):
    db = ctx.db
    fresh = db.query(Quote).filter(Quote.notified_customer_at.is_(None)).all()
    by_job: dict[str, list[Quote]] = {}
    for q in fresh:
        by_job.setdefault(q.job_id, []).append(q)
    for job_id, quotes in by_job.items():
        job = db.get(Job, job_id)
        customer = _customer(db, job) if job else None
        if job is None or not customer or not customer.email:
            continue
        useful = [q for q in quotes if quote_kind(q) in ("quote", "visit")]
        if not useful:  # only declines so far: not worth an email on their own
            continue
        last = (db.query(Notification).filter(Notification.job_id == job.id, Notification.kind == "customer_digest",
                                              Notification.status.in_(("sent", "test"))).order_by(Notification.created_at.desc()).first())
        if last and ctx.now - last.created_at < DIGEST_EVERY:
            continue
        sections, offers = [], 0
        got = [q for q in quotes if quote_kind(q) == "quote"]
        if got:
            sections.append("Quotes\n" + "\n".join(f"  - {db.get(Company, q.company_id).name}: {describe_price(q.price_type, q.price_amount, q.price_max)}"
                                                   for q in got))
        visits = [q for q in quotes if quote_kind(q) == "visit"]
        if visits:
            lines = []
            for q in visits:
                offers += 1
                lines.append(f"  - {db.get(Company, q.company_id).name} offered: {'; '.join(window_labels(q.visit_windows))}\n"
                             f"    Choose a time: {_link(ctx, f'/j/{job.public_token}/visit/{q.id}') or 'see your progress page'}")
            sections.append("Would like to see the job first\n" + "\n".join(lines))
        declined = [q for q in quotes if quote_kind(q) == "decline"]
        if declined:
            sections.append(f"Can't help this time: {len(declined)} supplier(s)")
        progress = _link(ctx, f"/j/{job.public_token}")
        body = (f"Hi {customer.name or 'there'},\n\nHere's what's new on your {job.category} quote request (ref {job.ref_prefix}):\n\n"
                + "\n\n".join(sections) + (f"\n\nSee everything in one place: {progress}\n" if progress else "\n") + "\nQuoteschmote\n")
        parts = [f"{len(got)} quote(s)"] if got else []
        parts += [f"{offers} visit offer(s)"] if offers else []
        ids = [q.id for q in quotes]

        def mark(ids=ids):
            for q in db.query(Quote).filter(Quote.id.in_(ids)).all():
                q.notified_customer_at = ctx.now
            db.commit()

        yield Planned("customer", "customer_digest", customer.email, f"New on your quote request: {', '.join(parts)} [Ref: {job.ref_prefix}]",
                      body, job.id, None, mark, needs_link=True)


# ---- customer: choose a visit time ----------------------------------------------------------------------------------
def _pick_reminders(ctx: Ctx):
    db = ctx.db
    for q in db.query(Quote).filter(Quote.kind == "visit", Quote.notified_customer_at.isnot(None), Quote.windows_declined.isnot(True)).all():
        reply = db.get(ReplyEmail, q.reply_email_id)
        rfq = db.get(RfqSend, reply.rfq_send_id)
        job = db.get(Job, q.job_id)
        customer = _customer(db, job)
        if not customer or not customer.email or db.query(Visit).filter_by(rfq_send_id=rfq.id, status="confirmed").count():
            continue
        future = [w for w in q.visit_windows or [] if isinstance(w, dict) and w.get("start") and timeutil.parse_local(w["start"]) > ctx.local]
        if not future:
            continue
        waited = ctx.now - q.notified_customer_at
        kind = None
        if waited >= PICK_REMINDER_AFTER[0] and not already_sent(db, "pick_reminder_1", rfq_send_id=rfq.id):
            kind = "pick_reminder_1"
        elif waited >= PICK_REMINDER_AFTER[1] and not already_sent(db, "pick_reminder_2", rfq_send_id=rfq.id):
            kind = "pick_reminder_2"
        if not kind:
            continue
        company = db.get(Company, q.company_id)
        link = _link(ctx, f"/j/{job.public_token}/visit/{q.id}")
        body = (f"Hi {customer.name or 'there'},\n\n{company.name} offered times to see your {job.category} job:\n"
                + "\n".join(f"  - {w['label']}" for w in future) + f"\n\nPick one (or ask for other times): {link or 'see your progress page'}\n\n"
                "The sooner you choose, the sooner you get a firm price.\n\nQuoteschmote\n")
        yield Planned("customer", kind, customer.email, f"Choose a time for {company.name}'s visit [Ref: {job.ref_prefix}]", body, job.id, rfq.id,
                      needs_link=True)


# ---- visit reminders and post-visit nudges --------------------------------------------------------------------------
def _visits(ctx: Ctx):
    db = ctx.db
    for visit in db.query(Visit).filter(Visit.status == "confirmed").all():
        start, end = timeutil.parse_local(visit.start_local), timeutil.parse_local(visit.end_local)
        rfq = db.get(RfqSend, visit.rfq_send_id)
        if not _reachable(rfq, ctx):
            continue
        job, company = db.get(Job, visit.job_id), db.get(Company, visit.company_id)
        customer = _customer(db, job)
        # reminders: from 24 hours before until the visit starts
        if ctx.local < start <= ctx.local + VISIT_REMINDER_WINDOW:
            if not already_sent(db, "visit_reminder_supplier", rfq_send_id=rfq.id):
                body = (f"Hi {company.name} team,\n\nA reminder of tomorrow's site visit: {visit.label} at {job.location_text} "
                        f"({customer.name if customer and customer.name else 'the customer'}"
                        f"{', ' + customer.mobile if customer and customer.mobile else ''}).\n\nThanks,\nQuoteschmote\n")
                yield Planned("supplier", "visit_reminder_supplier", rfq.recipient_email, f"Reminder: site visit {visit.label} [Ref: {job.ref_prefix}]",
                              body, job.id, rfq.id)
            if customer and customer.email and not already_sent(db, "visit_reminder_customer", rfq_send_id=rfq.id):
                body = (f"Hi {customer.name or 'there'},\n\nA reminder that {company.name} is visiting to see your {job.category} job: "
                        f"{visit.label} at {job.location_text}.\n\nNeed to change it? {_link(ctx, f'/j/{job.public_token}/visit/{visit.quote_id}') or ''}\n\n"
                        "Quoteschmote\n")
                yield Planned("customer", "visit_reminder_customer", customer.email, f"Reminder: {company.name} visits {visit.label}", body, job.id, rfq.id)
        # after the visit: ask the supplier for their quote, unless they have already sent one
        quote = db.query(Quote).filter_by(job_id=job.id, company_id=company.id).first()
        if quote is not None and quote_kind(quote) == "quote":
            continue
        since = ctx.local - end
        if since < POST_VISIT_AFTER[0] or since > STOP_POST_VISIT_AFTER:
            continue
        kind = "post_visit_1" if not already_sent(db, "post_visit_1", rfq_send_id=rfq.id) else (
            "post_visit_2" if since >= POST_VISIT_AFTER[1] and not already_sent(db, "post_visit_2", rfq_send_id=rfq.id) else None)
        if kind:
            url = _link(ctx, f"/q/{rfq.response_token}/quote") if rfq.response_token else ""
            body = (f"Hi {company.name} team,\n\nThanks for visiting the {job.category} job at {job.location_text} ({visit.label}). "
                    f"When you've priced it, you can send your quote here in about a minute:\n{url or 'just reply to this email'}\n\n"
                    f"Thanks,\n{_sign(customer)}\n")
            yield Planned("supplier", kind, rfq.recipient_email, f"Your quote for the site visit [Ref: {job.ref_prefix}]", body, job.id, rfq.id)


# ---- customer: quotes waiting ---------------------------------------------------------------------------------------
def _quotes_waiting(ctx: Ctx):
    db = ctx.db
    by_job: dict[str, list[Quote]] = {}
    for q in db.query(Quote).filter(Quote.notified_customer_at.isnot(None)).all():
        if quote_kind(q) == "quote":
            by_job.setdefault(q.job_id, []).append(q)
    for job_id, quotes in by_job.items():
        job = db.get(Job, job_id)
        customer = _customer(db, job)
        if not customer or not customer.email or already_sent(db, "quotes_waiting", job_id=job_id):
            continue
        if ctx.now - min(q.notified_customer_at for q in quotes) < QUOTES_WAITING_AFTER:
            continue
        link = _link(ctx, f"/j/{job.public_token}")
        body = (f"Hi {customer.name or 'there'},\n\nYou have {len(quotes)} quote(s) waiting for your {job.category} job (ref {job.ref_prefix}).\n"
                f"{('Compare them here: ' + link) if link else ''}\n\nQuoteschmote\n")
        yield Planned("customer", "quotes_waiting", customer.email, f"{len(quotes)} quote(s) waiting for you [Ref: {job.ref_prefix}]", body, job.id,
                      needs_link=True)


RULES = (_digest, _chases, _pick_reminders, _visits, _quotes_waiting)  # most useful to the customer first, so a daily cap trims the least important


# ---- running -------------------------------------------------------------------------------------------------------
def plan(db, now: datetime.datetime | None = None) -> list[Planned]:
    """Everything that is due right now. Pure planning: nothing is sent or recorded."""
    ctx = Ctx(db, now or datetime.datetime.utcnow(), config.PUBLIC_BASE_URL, outbound.mode())
    out: list[Planned] = []
    for rule in RULES:
        for item in rule(ctx):
            if item.audience == "supplier" and is_suppressed(db, item.to_email):
                continue
            if item.needs_link and not ctx.base_url:
                continue
            out.append(item)
    return out


def config_warnings() -> list[str]:
    """Settings problems that quietly hold emails back. Shown on the Outbox page and by the command line."""
    warnings = []
    if not config.PUBLIC_BASE_URL:
        warnings.append("PUBLIC_BASE_URL is not set in .env (for example https://quoteschmote.com), so customer updates that depend on a "
                        "link (new quotes, choose a visit time, quotes waiting) are held back, and supplier emails carry no response link.")
    if outbound.mode() == "test" and not outbound.test_recipient():
        warnings.append("OUTBOUND_MODE=test needs TEST_RECIPIENT set to your own address.")
    return warnings


def run(db, *, send: bool = False, now: datetime.datetime | None = None) -> Report:
    """Plan, and (if `send`) deliver what is due. In dry_run mode nothing is delivered whatever `send` says."""
    now = now or datetime.datetime.utcnow()
    due = plan(db, now)
    report = Report(planned=due)
    if not send or outbound.mode() == "dry_run":
        report.note = "preview only" if not send else "dry_run mode: nothing is sent"
        return report
    if not in_business_hours(timeutil.utc_to_local(now)):
        report.note = "outside business hours (08:00 to 18:00, Mon to Sat): nothing sent"
        return report
    sent_today = db.query(Notification).filter(Notification.status.in_(("sent", "test")), Notification.created_at >= now - DAY).count()
    room = max(0, min(MAX_PER_RUN, DAILY_CAP - sent_today))
    if room == 0:
        report.note = "daily follow-up cap reached"
    for item in due[:room]:
        note = send_notification(db, audience=item.audience, kind=item.kind, to_email=item.to_email, subject=item.subject, body=item.body,
                                 job_id=item.job_id, rfq_send_id=item.rfq_send_id, record_dry_run=False, created_at=now)
        if note is not None and note.status in ("sent", "test"):
            report.sent += 1
            if item.on_sent:
                item.on_sent()
    if len(due) > room and not report.note:
        report.note = f"{len(due) - room} more are due and will go out on the next run"
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview or send the automated follow-ups that are due.")
    parser.add_argument("--send", action="store_true", help="really send (still respects OUTBOUND_MODE and business hours)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    db = get_session()
    try:
        report = run(db, send=args.send)
    finally:
        db.close()
    print(f"mode={outbound.mode()}  due={len(report.planned)}  sent={report.sent}  {report.note}")
    for warning in config_warnings():
        print(f"  WARNING: {warning}")
    for item in report.planned:
        print(f"  [{item.audience}] {item.kind} -> {item.to_email} | {item.subject}")


if __name__ == "__main__":
    main()
