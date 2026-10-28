"""The customer's side of site visits: pick one of the times a supplier offered, share contact details for that visit only, and add it to
a calendar. Reached from the customer's progress page (/j/<token>/visit/<quote id>); the progress token is the credential.

Nothing about the customer is shared with a supplier until they tick the consent box here, and then only their first name, mobile and
the job address, for that one supplier."""
from __future__ import annotations

import html

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from db.models import Company, Job, Quote, ReplyEmail, RfqSend, User, Visit
from db.session import get_session
from server.supplier_response import _not_found, _page
from server.web_chat import SlidingWindow, client_ip, public_base
from services import timeutil
from services.job_state import is_open
from services.notify import send_notification
from services.visit_messages import (
    visit_cancelled_for_supplier,
    visit_confirmed_for_customer,
    visit_confirmed_for_supplier,
    visit_invite,
    windows_declined_for_supplier,
)

router = APIRouter()
view_limiter = SlidingWindow(limit=120, seconds=600)
action_limiter = SlidingWindow(limit=20, seconds=600)
e = html.escape


def _load(db, public_token: str, quote_id: str):
    """(job, quote, rfq, company, customer) for a visit request on this customer's job, else None."""
    if not public_token or len(public_token) > 64:
        return None
    job = db.query(Job).filter_by(public_token=public_token).first()
    quote = db.get(Quote, quote_id) if job else None
    if quote is None or quote.job_id != job.id or quote.kind != "visit":
        return None
    reply = db.get(ReplyEmail, quote.reply_email_id)
    rfq = db.get(RfqSend, reply.rfq_send_id)
    return job, quote, rfq, db.get(Company, quote.company_id), (db.get(User, job.user_id) if job.user_id else None)


def _confirmed(db, rfq: RfqSend) -> Visit | None:
    return db.query(Visit).filter_by(rfq_send_id=rfq.id, status="confirmed").order_by(Visit.created_at.desc()).first()


def _selectable(quote: Quote) -> list[tuple[int, dict]]:
    """(index, window) for offered times still in the future. The index into the stored list is what the form submits, so it stays
    stable even if a time passes while the page is open. Old free-text windows can't be booked and are skipped."""
    now = timeutil.local_now()
    out = []
    for i, w in enumerate(quote.visit_windows or []):
        if isinstance(w, dict) and w.get("start") and timeutil.parse_local(w["start"]) > now:
            out.append((i, w))
    return out


def _gate(request: Request, public_token: str, quote_id: str, acting: bool = False, allow_ended: bool = False):
    ip = client_ip(request)
    if not view_limiter.allow(ip) or (acting and not action_limiter.allow(ip)):
        return _page("Slow down", "<h2>Too many requests</h2><p class='muted'>Please wait a few minutes and try again.</p>", 429)
    db = get_session()
    loaded = _load(db, public_token, quote_id)
    if loaded is None:
        db.close()
        return _not_found()
    if not allow_ended and not is_open(loaded[0]):
        db.close()
        return _page("Request ended", f"<p><a class='back' href='/j/{public_token}'>&larr; Back to your request</a></p>"
                     "<h2>This request has ended</h2><p class='muted'>It has been closed or awarded, so visit times can't be changed.</p>")
    return db, loaded


def _supplier_link(request: Request, rfq: RfqSend) -> str | None:
    return f"{public_base(request)}/q/{rfq.response_token}" if rfq.response_token else None


def _page_for(db, request: Request, public_token: str, quote_id: str, job, quote, rfq, company, customer, note: str = "") -> HTMLResponse:
    back = f"<p><a class='back' href='/j/{public_token}'>&larr; Back to your request</a></p>"
    base = f"/j/{public_token}/visit/{quote_id}"
    visit = _confirmed(db, rfq)
    if visit:
        return _page("Site visit", f"""{back}<div class='notice ok'><b>Visit booked with {e(company.name)}</b><br>{e(visit.label)}<br>{e(job.location_text)}</div>
<a class='btn primary' href='{base}/visit.ics'>Add to calendar</a>
<form method='post' action='{base}/cancel'><button class='btn quiet' type='submit'>Cancel this visit</button></form>
<p class='fine'>{e(company.name)} has your first name, mobile and the job address for this visit, and nothing else.</p>""")
    options = _selectable(quote)
    if quote.windows_declined:
        return _page("Site visit", f"{back}<div class='notice'><b>We've asked {e(company.name)} for other times.</b><br>"
                                   "You'll see them here as soon as they reply.</div>")
    if not options:
        return _page("Site visit", f"{back}<div class='notice'>None of the times {e(company.name)} offered are still available.</div>"
                                   f"<form method='post' action='{base}/other'><button class='btn primary' type='submit'>Ask for new times</button></form>")
    radios = "".join(f'<label class="radio"><input type="radio" name="slot" value="{i}"> {e(w["label"])}</label>' for i, w in options)
    shared = ", ".join(x for x in (customer.name if customer and customer.name else "", customer.mobile if customer and customer.mobile else "",
                                   job.location_text) if x)
    err = f"<p class='error' role='alert'>{e(note)}</p>" if note else ""
    return _page("Choose a time", f"""{back}<h2>Choose a time for {e(company.name)}'s visit</h2><p class='muted'>{e(job.title)}</p>{err}
<form method='post' action='{base}'>
<div class='f'><div class='radios'>{radios}</div></div>
<label class='radio consent'><input type='checkbox' name='consent' value='1'> I'm happy to share my first name, mobile and the job address
({e(shared)}) with {e(company.name)} so they can visit. Nothing else is shared.</label>
<button class='btn primary' type='submit'>Confirm this time</button></form>
<form method='post' action='{base}/other'><button class='btn quiet' type='submit'>None of these suit</button></form>""")


@router.get("/j/{public_token}/visit/{quote_id}", response_class=HTMLResponse)
def picker(request: Request, public_token: str, quote_id: str):
    gate = _gate(request, public_token, quote_id)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (job, quote, rfq, company, customer) = gate
    try:
        return _page_for(db, request, public_token, quote_id, job, quote, rfq, company, customer)
    finally:
        db.close()


@router.post("/j/{public_token}/visit/{quote_id}", response_class=HTMLResponse)
def confirm(request: Request, public_token: str, quote_id: str, slot: str = Form(""), consent: str = Form("")):
    gate = _gate(request, public_token, quote_id, acting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (job, quote, rfq, company, customer) = gate
    try:
        offered = dict(_selectable(quote))
        try:
            window = offered.get(int(slot))
        except ValueError:
            window = None
        if window is None:
            return _page_for(db, request, public_token, quote_id, job, quote, rfq, company, customer, "Please choose one of the times.")
        if consent != "1":
            return _page_for(db, request, public_token, quote_id, job, quote, rfq, company, customer,
                             f"Please tick the box to share your details with {company.name}.")
        for old in db.query(Visit).filter_by(rfq_send_id=rfq.id, status="confirmed").all():
            old.status = "cancelled"  # only one confirmed visit per supplier
        visit = Visit(job_id=job.id, rfq_send_id=rfq.id, company_id=company.id, quote_id=quote.id, start_local=window["start"],
                      end_local=window["end"], label=window["label"], status="confirmed")
        db.add(visit)
        quote.windows_declined = False
        db.commit()

        subject, body = visit_confirmed_for_supplier(job, company, customer, visit, _supplier_link(request, rfq))
        send_notification(db, audience="supplier", kind="visit_confirmed", to_email=rfq.recipient_email, subject=subject, body=body,
                          job_id=job.id, rfq_send_id=rfq.id,
                          extra_files=[visit_invite(visit, job, company, for_supplier=True, customer_first_name=customer.name if customer else None)])
        if customer and customer.email:
            subject, body = visit_confirmed_for_customer(job, company, visit, f"{public_base(request)}/j/{public_token}")
            send_notification(db, audience="customer", kind="visit_confirmed_customer", to_email=customer.email, subject=subject, body=body,
                              job_id=job.id, rfq_send_id=rfq.id, extra_files=[visit_invite(visit, job, company, for_supplier=False)])
        return RedirectResponse(f"/j/{public_token}/visit/{quote_id}", status_code=303)
    finally:
        db.close()


@router.post("/j/{public_token}/visit/{quote_id}/other", response_class=HTMLResponse)
def other_times(request: Request, public_token: str, quote_id: str):
    gate = _gate(request, public_token, quote_id, acting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (job, quote, rfq, company, customer) = gate
    try:
        if not quote.windows_declined and _confirmed(db, rfq) is None:
            quote.windows_declined = True
            db.commit()
            subject, body = windows_declined_for_supplier(job, company, _supplier_link(request, rfq))
            send_notification(db, audience="supplier", kind="visit_other_times", to_email=rfq.recipient_email, subject=subject, body=body,
                              job_id=job.id, rfq_send_id=rfq.id)
        return RedirectResponse(f"/j/{public_token}/visit/{quote_id}", status_code=303)
    finally:
        db.close()


@router.post("/j/{public_token}/visit/{quote_id}/cancel", response_class=HTMLResponse)
def cancel(request: Request, public_token: str, quote_id: str):
    gate = _gate(request, public_token, quote_id, acting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (job, quote, rfq, company, customer) = gate
    try:
        visit = _confirmed(db, rfq)
        if visit:
            visit.status = "cancelled"
            quote.windows_declined = True  # the supplier is asked for new times
            db.commit()
            subject, body = visit_cancelled_for_supplier(job, company, visit, _supplier_link(request, rfq))
            send_notification(db, audience="supplier", kind="visit_cancelled", to_email=rfq.recipient_email, subject=subject, body=body,
                              job_id=job.id, rfq_send_id=rfq.id,
                              extra_files=[visit_invite(visit, job, company, for_supplier=True, customer_first_name=customer.name if customer else None,
                                                        cancelled=True)])
        return RedirectResponse(f"/j/{public_token}/visit/{quote_id}", status_code=303)
    finally:
        db.close()


@router.get("/j/{public_token}/visit/{quote_id}/visit.ics")
def ics(request: Request, public_token: str, quote_id: str):
    gate = _gate(request, public_token, quote_id, allow_ended=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (job, quote, rfq, company, customer) = gate
    try:
        visit = _confirmed(db, rfq)
        if visit is None:
            return _not_found()
        name, _mime, data = visit_invite(visit, job, company, for_supplier=False)
        return Response(data, media_type="text/calendar", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
    finally:
        db.close()
