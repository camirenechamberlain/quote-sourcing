"""The customer manages their own request from its progress page (/j/<token>/...): close it, choose a supplier, or add details and photos.
The progress token is the credential, as for the visit picker.

Ending a request (closing it or choosing a supplier) stops everything: remaining suppliers are told, booked visits are cancelled, supplier
response pages stop accepting quotes, and the automated follow-ups skip it."""
from __future__ import annotations

import datetime
import html
import mimetypes
import secrets
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from db.models import Company, Job, Quote, ReplyEmail, RfqSend, User, Visit
from db.session import get_session
from server.supplier_response import _not_found, _page, parse_money
from server.web_chat import (
    MAX_ATTACHMENTS_PER_REQUEST,
    MAX_PDFS_PER_REQUEST,
    SlidingWindow,
    client_ip,
    public_base,
)
from services import attachments as attachments_service
from services import documents, images
from services.job_messages import awarded_to_supplier, closed_for_supplier, not_chosen_for_supplier, update_for_supplier
from services.job_state import CLOSE_REASONS, is_open
from services.notify import send_notification
from services.quote_format import describe_price, quote_kind
from services.visit_messages import visit_invite

router = APIRouter()
view_limiter = SlidingWindow(limit=120, seconds=600)
action_limiter = SlidingWindow(limit=20, seconds=600)
update_limiter = SlidingWindow(limit=6, seconds=3600)  # each update can email every supplier, so keep it modest
MAX_UPDATES_PER_JOB = 5
MAX_NOTE = 1000
MAX_FILES_PER_UPDATE = 6
e = html.escape


# ---- plumbing -----------------------------------------------------------------------------------------------------
def _gate(request: Request, public_token: str, acting: bool = False, need_open: bool = True):
    """(db, job) for a valid progress token, or an HTMLResponse to return instead."""
    ip = client_ip(request)
    if not view_limiter.allow(ip) or (acting and not action_limiter.allow(ip)):
        return _page("Slow down", "<h2>Too many requests</h2><p class='muted'>Please wait a few minutes and try again.</p>", 429)
    if not public_token or len(public_token) > 64:
        return _not_found()
    db = get_session()
    job = db.query(Job).filter_by(public_token=public_token).first()
    if job is None:
        db.close()
        return _not_found()
    if need_open and not is_open(job):
        db.close()
        return _page("Request ended", f"<p><a class='back' href='/j/{public_token}'>&larr; Back to your request</a></p>"
                     "<h2>This request has ended</h2><p class='muted'>It has already been closed or awarded, so it can't be changed.</p>")
    return db, job


def _back(token: str) -> str:
    return f"<p><a class='back' href='/j/{e(token)}'>&larr; Back to your request</a></p>"


def _customer(db, job: Job) -> User | None:
    return db.get(User, job.user_id) if job.user_id else None


def _contacted(db, job: Job) -> list[tuple[RfqSend, Company, Quote | None]]:
    """Every supplier we emailed for this job, with their latest response (if any)."""
    out = []
    for rfq in db.query(RfqSend).filter(RfqSend.job_id == job.id, RfqSend.status == "sent").all():
        reply = db.query(ReplyEmail).filter_by(rfq_send_id=rfq.id).first()
        quote = db.query(Quote).filter_by(reply_email_id=reply.id).first() if reply else None
        out.append((rfq, db.get(Company, rfq.company_id), quote))
    return out


def _supplier_link(request: Request, rfq: RfqSend) -> str | None:
    return f"{public_base(request)}/q/{rfq.response_token}" if rfq.response_token else None


def _cancel_visits(db, request, job, customer, rfq, company) -> None:
    for visit in db.query(Visit).filter_by(rfq_send_id=rfq.id, status="confirmed").all():
        visit.status = "cancelled"
    db.commit()


# ---- close ----------------------------------------------------------------------------------------------------------
@router.get("/j/{public_token}/close", response_class=HTMLResponse)
def close_form(request: Request, public_token: str):
    gate = _gate(request, public_token)
    if isinstance(gate, HTMLResponse):
        return gate
    db, job = gate
    try:
        radios = "".join(f'<label class="radio"><input type="radio" name="reason" value="{k}"> {e(v)}</label>' for k, v in CLOSE_REASONS.items())
        return _page("Close request", f"""{_back(public_token)}<h2>Close this request?</h2>
<p class='muted'>We'll let the suppliers we contacted know, cancel any booked site visits, and stop all follow-ups. You can start a new request any time.</p>
<form method='post' action='/j/{e(public_token)}/close'><div class='f'><span class='label'>Why? (optional)</span><div class='radios'>{radios}</div></div>
<button class='btn primary' type='submit'>Close this request</button></form>""")
    finally:
        db.close()


def _end_job(db, request: Request, job: Job, *, chosen: Company | None = None, price: float | None = None) -> None:
    """Notify every contacted supplier appropriately, cancel their visits, and send nothing twice."""
    customer = _customer(db, job)
    for rfq, company, quote in _contacted(db, job):
        had_visit = db.query(Visit).filter_by(rfq_send_id=rfq.id, status="confirmed").first()
        if chosen is not None and company.id == chosen.id:
            kind, (subject, body) = "job_awarded", awarded_to_supplier(job, company, customer, price, _supplier_link(request, rfq))
            files = []
        else:
            if quote is not None and quote_kind(quote) == "decline":
                continue  # they already said no: no need to email them again
            kind = "job_not_chosen" if chosen is not None else "job_closed"
            subject, body = (not_chosen_for_supplier if chosen is not None else closed_for_supplier)(job, company, customer)
            files = [visit_invite(had_visit, job, company, for_supplier=True, cancelled=True)] if had_visit else []
            _cancel_visits(db, request, job, customer, rfq, company)
        send_notification(db, audience="supplier", kind=kind, to_email=rfq.recipient_email, subject=subject, body=body, job_id=job.id,
                          rfq_send_id=rfq.id, extra_files=files or None)


@router.post("/j/{public_token}/close", response_class=HTMLResponse)
def close(request: Request, public_token: str, reason: str = Form("")):
    gate = _gate(request, public_token, acting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, job = gate
    try:
        job.status = "CLOSED"
        job.closed_reason = reason if reason in CLOSE_REASONS else "other"
        job.closed_at = datetime.datetime.utcnow()
        db.commit()
        _end_job(db, request, job)
        return RedirectResponse(f"/j/{public_token}", status_code=303)
    finally:
        db.close()


# ---- choose a supplier -------------------------------------------------------------------------------------------------
def _load_quote(db, job: Job, quote_id: str):
    quote = db.get(Quote, quote_id)
    if quote is None or quote.job_id != job.id or quote_kind(quote) != "quote":
        return None
    reply = db.get(ReplyEmail, quote.reply_email_id)
    return quote, db.get(RfqSend, reply.rfq_send_id), db.get(Company, quote.company_id)


@router.get("/j/{public_token}/choose/{quote_id}", response_class=HTMLResponse)
def choose_form(request: Request, public_token: str, quote_id: str, note: str = ""):
    gate = _gate(request, public_token)
    if isinstance(gate, HTMLResponse):
        return gate
    db, job = gate
    try:
        loaded = _load_quote(db, job, quote_id)
        if loaded is None:
            return _not_found()
        quote, rfq, company = loaded
        customer = _customer(db, job)
        shared = ", ".join(x for x in (customer.name if customer and customer.name else "", customer.mobile if customer and customer.mobile else "",
                                       customer.email if customer and customer.email else "", job.location_text) if x)
        details = [f"<li><b>Price:</b> {e(describe_price(quote.price_type, quote.price_amount, quote.price_max))}</li>"]
        for label, value in (("Included", ", ".join(quote.scope_included or [])), ("Not included", ", ".join(quote.exclusions or [])),
                             ("Timeline", quote.timeline_text or ""), ("Warranty", quote.warranty_notes or "")):
            if value:
                details.append(f"<li><b>{label}:</b> {e(value)}</li>")
        err = f"<p class='error' role='alert'>{e(note)}</p>" if note else ""
        default = f"{quote.price_amount:g}" if quote.price_amount else ""
        return _page("Choose supplier", f"""{_back(public_token)}<h2>Go with {e(company.name)}?</h2><ul class='facts'>{''.join(details)}</ul>{err}
<form method='post' action='/j/{e(public_token)}/choose/{e(quote_id)}'>
<div class='f'><label for='price'>Agreed price ($, so we can record the outcome)</label><input id='price' name='price' type='text' inputmode='decimal' value='{e(default)}'></div>
<label class='radio consent'><input type='checkbox' name='consent' value='1'> I'm happy to share my contact details ({e(shared)}) with {e(company.name)} so they can get in touch.</label>
<button class='btn primary' type='submit'>Choose {e(company.name)}</button></form>
<p class='fine'>The other suppliers will be told you went another way, and any booked visits will be cancelled.</p>""")
    finally:
        db.close()


@router.post("/j/{public_token}/choose/{quote_id}", response_class=HTMLResponse)
def choose(request: Request, public_token: str, quote_id: str, price: str = Form(""), consent: str = Form("")):
    gate = _gate(request, public_token, acting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, job = gate
    try:
        loaded = _load_quote(db, job, quote_id)
        if loaded is None:
            return _not_found()
        quote, rfq, company = loaded
        if consent != "1":
            return RedirectResponse(f"/j/{public_token}/choose/{quote_id}?note=" + quote_plus(f"Please tick the box to share your details with {company.name[:60]}."), status_code=303)
        agreed = parse_money(price) if price.strip() else quote.price_amount
        if price.strip() and agreed is None:
            return RedirectResponse(f"/j/{public_token}/choose/{quote_id}?note=" + quote_plus("Please enter the price as a number, or leave it blank."), status_code=303)
        for other in db.query(Quote).filter_by(job_id=job.id).all():
            other.is_winner = other.id == quote.id
        job.status, job.chosen_company_id, job.final_price = "AWARDED", company.id, agreed
        job.closed_at = datetime.datetime.utcnow()
        db.commit()
        _end_job(db, request, job, chosen=company, price=agreed)
        return RedirectResponse(f"/j/{public_token}", status_code=303)
    finally:
        db.close()


# ---- add details and photos ---------------------------------------------------------------------------------------------
@router.get("/j/{public_token}/update", response_class=HTMLResponse)
def update_form(request: Request, public_token: str, note: str = ""):
    gate = _gate(request, public_token)
    if isinstance(gate, HTMLResponse):
        return gate
    db, job = gate
    try:
        contacted = len(_contacted(db, job))
        warning = (f"<p class='muted'>We'll email your note and any new photos to the {contacted} supplier{'s' if contacted != 1 else ''} already "
                   "contacted.</p>") if contacted else "<p class='muted'>These will be included when we contact suppliers.</p>"
        err = f"<p class='error' role='alert'>{e(note)}</p>" if note else ""
        return _page("Add details", f"""{_back(public_token)}<h2>Add details or photos</h2>{warning}{err}
<form method='post' action='/j/{e(public_token)}/update' enctype='multipart/form-data'>
<div class='f'><label for='note'>What would you like to add?</label><textarea id='note' name='note' rows='4' maxlength='{MAX_NOTE}'></textarea></div>
<div class='f'><label for='files'>Photos or PDFs (optional)</label><input id='files' name='files' type='file' accept='image/*,application/pdf' multiple></div>
<button class='btn primary' type='submit'>Add to my request</button></form>""")
    finally:
        db.close()


@router.post("/j/{public_token}/update", response_class=HTMLResponse)
def update(request: Request, public_token: str, note: str = Form(""), files: list[UploadFile] = File(default=[])):
    gate = _gate(request, public_token, acting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, job = gate
    try:
        def back(message: str) -> RedirectResponse:
            return RedirectResponse(f"/j/{public_token}/update?note=" + quote_plus(message[:300]), status_code=303)

        text = "".join(ch for ch in note if ch == "\n" or ch >= " ").strip()[:MAX_NOTE]
        uploads = [f for f in files if f is not None and (f.filename or f.size)]
        if not text and not uploads:
            return back("Please write a note or choose a file.")
        if not update_limiter.allow(public_token):
            return back("You've made several updates recently. Please wait a while before adding more.")
        if job.description.count("\n\nUpdate ") >= MAX_UPDATES_PER_JOB:
            return back("This request has reached its limit of updates.")
        existing = list(job.attachment_paths or [])
        if len(uploads) > MAX_FILES_PER_UPDATE or len(existing) + len(uploads) > MAX_ATTACHMENTS_PER_REQUEST:
            return back(f"A request can include up to {MAX_ATTACHMENTS_PER_REQUEST} attachments.")

        dest = attachments_service.job_attachment_dir(job.id)
        saved: list[Path] = []
        pdfs = sum(1 for p in existing if str(p).lower().endswith(".pdf"))
        try:
            for f in uploads:
                raw = f.file.read(documents.MAX_PDF_BYTES + 1)
                if documents.is_pdf(raw):  # decided by content, never by the file name or claimed type
                    pdfs += 1
                    if pdfs > MAX_PDFS_PER_REQUEST:
                        raise documents.BadPdf(f"A request can include up to {MAX_PDFS_PER_REQUEST} PDFs.")
                    saved.append(documents.save_sanitised_pdf(raw, dest, secrets.token_hex(4), f.filename))
                else:
                    saved.append(images.save_sanitised(raw, dest, secrets.token_hex(6)))
            if attachments_service.total_size(existing + [str(p) for p in saved]) > attachments_service.MAX_TOTAL_BYTES:
                raise images.BadImage(f"Attachments can total up to {attachments_service.MAX_TOTAL_BYTES // 1_000_000}MB.")
        except (images.BadImage, documents.BadPdf) as exc:
            for p in saved:
                p.unlink(missing_ok=True)
            return back(str(exc))

        stamp = datetime.date.today().strftime("%d %b %Y").lstrip("0")
        if text:
            job.description = f"{job.description}\n\nUpdate {stamp}: {text}"
        job.attachment_paths = existing + [str(p) for p in saved]  # reassigned, so the JSON column change is saved
        db.commit()

        names = [f"update-photo-{i}{p.suffix.lower()}" if p.suffix.lower() != ".pdf" else attachments_service.display_name(p, 0) for i, p in enumerate(saved, 1)]
        blobs = [(n, mimetypes.guess_type(n)[0] or "application/octet-stream", p.read_bytes()) for n, p in zip(names, saved)]
        customer = _customer(db, job)
        for rfq, company, quote in _contacted(db, job):
            if quote is not None and quote_kind(quote) == "decline":
                continue
            subject, body = update_for_supplier(job, company, customer, text, names, _supplier_link(request, rfq))
            send_notification(db, audience="supplier", kind="job_update", to_email=rfq.recipient_email, subject=subject, body=body,
                              job_id=job.id, rfq_send_id=rfq.id, extra_files=blobs or None)
        return RedirectResponse(f"/j/{public_token}?updated=1", status_code=303)
    finally:
        db.close()
