"""The supplier's no-login response page (/q/<token>): quote, ask for a site visit, or decline, in about a minute.

The link in the RFQ email carries an unguessable per-request token, which is the only credential. Pages are plain server-rendered
HTML and forms (no JavaScript, no account), so they work on any phone. A response is stored exactly like an emailed reply (a
ReplyEmail plus a structured Quote), so the Compare Quotes page and the customer's progress page pick it up with no extra work.
Submitting again from the same link replaces the earlier response, so a supplier can revise a price.
"""
from __future__ import annotations

import datetime
import html
import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from db.models import Company, Job, Quote, ReplyEmail, RfqSend, User, Visit
from db.session import get_session
from server import assets
from server.web_chat import SlidingWindow, client_ip
from services import attachments as attachments_service
from services import timeutil
from services.quote_format import describe_price, window_labels
from services.visit_messages import visit_invite

router = APIRouter()

DEFAULT_CURRENCY = "AUD"  # TODO: derive from the job's country once the platform is country-aware
MAX_PRICE = 10_000_000
MAX_TEXT = 1000
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
DECLINE_REASONS = {
    "out_of_area": "Outside the area we cover",
    "too_busy": "Too busy right now",
    "wrong_trade": "Not the type of work we do",
    "other": "Another reason",
}
PRICE_TYPES = {"fixed": "Fixed price", "range": "Price range", "hourly": "Hourly rate"}

view_limiter = SlidingWindow(limit=120, seconds=600)
submit_limiter_ip = SlidingWindow(limit=15, seconds=600)
submit_limiter_token = SlidingWindow(limit=20, seconds=3600)

e = html.escape


# --- Helpers -------------------------------------------------------------------------------------------------
def clean_text(value: str | None, limit: int = MAX_TEXT) -> str:
    """Trim, drop control characters, keep line breaks, cap the length."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def parse_money(value: str | None) -> float | None:
    """'$1,250.50' -> 1250.5. None if blank or not a sensible positive amount."""
    raw = re.sub(r"[\s$]", "", value or "")
    if not re.fullmatch(r"(\d+|\d{1,3}(,\d{3})+)(\.\d{1,2})?", raw):  # commas only as proper thousands separators
        return None
    amount = float(raw.replace(",", ""))
    return amount if 0 < amount <= MAX_PRICE else None


def lines(value: str) -> list[str]:
    return [clean_text(line, 200) for line in (value or "").split("\n") if clean_text(line, 200)][:10]


def _load(db, token: str):
    """(rfq, job, company) for a live request, or None. Only requests that were actually sent have a usable link."""
    if not TOKEN_RE.match(token or ""):
        return None
    rfq = db.query(RfqSend).filter_by(response_token=token, status="sent").first()
    if rfq is None:
        return None
    return rfq, db.get(Job, rfq.job_id), db.get(Company, rfq.company_id)


def _existing(db, rfq: RfqSend) -> Quote | None:
    reply = db.query(ReplyEmail).filter_by(gmail_message_id=f"web-{rfq.id}").first()
    return db.query(Quote).filter_by(reply_email_id=reply.id).first() if reply else None


def save_response(db, rfq: RfqSend, job: Job, company: Company, *, kind: str, price_type: str = "unknown",
                  price: float | None = None, price_max: float | None = None, included: str = "", excluded: str = "",
                  timeline: str = "", warranty: str = "", notes: str = "", visit_windows: list[dict] | None = None,
                  decline_reason: str | None = None) -> Quote:
    """Store (or replace) the supplier's web response as a ReplyEmail + Quote, so the rest of the app treats it like an email reply."""
    visit_windows = visit_windows or []
    price_text = describe_price(price_type, price, price_max)
    if kind == "quote":
        summary = f"Quote: {price_text}" + (f". {notes}" if notes else "")
    elif kind == "visit":
        summary = "Wants to see the job first. Could visit: " + "; ".join(window_labels(visit_windows)) + (f". {notes}" if notes else "")
    else:
        summary = f"Declined: {DECLINE_REASONS.get(decline_reason or 'other', 'Another reason')}" + (f". {notes}" if notes else "")
    body_lines = [f"[Response from the supplier page] {summary}"]
    for label, text in (("Included", included), ("Excluded", excluded), ("Timeline", timeline), ("Warranty", warranty)):
        if text:
            body_lines.append(f"{label}: {text}")

    reply = db.query(ReplyEmail).filter_by(gmail_message_id=f"web-{rfq.id}").first()
    if reply is None:
        reply = ReplyEmail(rfq_send_id=rfq.id, gmail_message_id=f"web-{rfq.id}", gmail_thread_id=rfq.gmail_thread_id,
                           from_email=rfq.recipient_email, subject=f"Response page: {rfq.subject}"[:300], raw_body_text="", source="web")
        db.add(reply)
        db.flush()
    reply.received_at = datetime.datetime.utcnow()
    reply.raw_body_text = "\n".join(body_lines)

    quote = db.query(Quote).filter_by(reply_email_id=reply.id).first()
    if quote is None:
        quote = Quote(reply_email_id=reply.id, job_id=job.id, company_id=company.id)
        db.add(quote)
    quote.kind = kind
    quote.is_quote = kind == "quote"
    quote.price_amount = price if kind == "quote" else None
    quote.price_max = price_max if kind == "quote" and price_type == "range" else None
    quote.currency = DEFAULT_CURRENCY if kind == "quote" else None
    quote.price_type = price_type if kind == "quote" else "unknown"
    quote.scope_included = lines(included)
    quote.exclusions = lines(excluded)
    quote.timeline_text = timeline or None
    quote.warranty_notes = warranty or None
    quote.confidence = "high"  # the supplier typed these fields themselves; nothing was guessed from free text
    quote.summary = summary[:500]
    quote.decline_reason = decline_reason if kind == "decline" else None
    quote.visit_windows = visit_windows if kind == "visit" else []
    quote.windows_declined = False  # a fresh response clears any earlier "those times don't suit"
    quote.llm_raw_response = None
    db.commit()
    return quote


# --- HTML ------------------------------------------------------------------------------------------------
def _page(title: str, inner: str, status: int = 200) -> HTMLResponse:
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, nofollow">
<title>{e(title)}</title><link rel="stylesheet" href="{assets.asset('chat.css')}"><link rel="stylesheet" href="{assets.asset('supplier.css')}">
</head><body class="page"><main class="card supplier"><h1 class="brand"><span class="b1">quote</span><span class="b2">schmote</span></h1>
{inner}</main></body></html>"""
    return HTMLResponse(page, status_code=status, headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


def _not_found() -> HTMLResponse:
    return _page("Not found", "<h2>This link isn't active</h2><p class='muted'>It may have expired or been mistyped. If you received a "
                 "quote request by email, you can simply reply to that email.</p>", 404)


def _brief(db, job: Job, rfq: RfqSend) -> str:
    customer = db.get(User, job.user_id) if job.user_id else None
    on_behalf = f" on behalf of {e(customer.name)}" if customer and customer.name else ""
    facts = [f"<li><b>Where:</b> {e(job.location_text)}</li>"]
    if job.project_type:
        facts.append(f"<li><b>Type:</b> {e(job.project_type)}</li>")
    if job.timeframe:
        facts.append(f"<li><b>When:</b> {e(job.timeframe)}</li>")
    if job.deadline:
        facts.append(f"<li><b>Reply by:</b> {e(job.deadline)}</li>")
    files = attachments_service.supplier_file_names(job.attachment_paths)
    if files:
        facts.append(f"<li><b>Attached to the email:</b> {e(', '.join(files))}</li>")
    return (f"<h2>{e(job.title)}</h2><p class='muted'>{e(job.category)} quote request{on_behalf} &middot; ref {e(rfq.reference_code)}</p>"
            f"<ul class='facts'>{''.join(facts)}</ul><div class='brief'>{e(job.description or '')}</div>")


def _actions(token: str) -> str:
    return (f"<a class='btn primary' href='/q/{token}/quote'>Send my quote</a>"
            f"<a class='btn' href='/q/{token}/visit'>I need to see the job first</a>"
            f"<a class='btn quiet' href='/q/{token}/decline'>Can't help with this one</a>")


def _landing(db, token: str, rfq: RfqSend, job: Job, company: Company, saved: bool) -> HTMLResponse:
    existing = _existing(db, rfq)
    banner = ""
    if existing:
        title = "Thanks, your response is with the customer." if saved else "You've already responded."
        banner = f"<div class='notice'><b>{title}</b><br>{e(existing.summary or '')}<br><span class='muted'>You can change it below.</span></div>"
    visit = db.query(Visit).filter_by(rfq_send_id=rfq.id, status="confirmed").order_by(Visit.created_at.desc()).first()
    if visit:  # the customer has confirmed a time: this is the one place the supplier sees the customer's contact details
        customer = db.get(User, job.user_id) if job.user_id else None
        who = e(customer.name) if customer and customer.name else "The customer"
        banner = (f"<div class='notice ok'><b>Site visit confirmed: {e(visit.label)}</b><br>{who}"
                  + (f" &middot; <a href='tel:{e(customer.mobile)}'>{e(customer.mobile)}</a>" if customer and customer.mobile else "")
                  + (f" &middot; {e(customer.email)}" if customer and customer.email else "")
                  + f"<br>{e(job.location_text)}<br><a href='/q/{token}/visit.ics'>Add to calendar</a><br>"
                  "<span class='muted'>After the visit, send your quote below so it reaches the customer straight away.</span></div>") + banner
    elif existing and existing.kind == "visit" and existing.windows_declined:
        banner = ("<div class='notice'><b>The customer can't make those times.</b><br>Please offer a couple of other times "
                  f"below.</div>") + banner
    ask = "<p><b>Change your response</b></p>" if existing else "<p><b>How would you like to respond?</b></p>"
    foot = "<p class='fine'>Prefer email? Just reply to the email you received. That works too. No account needed.</p>"
    return _page("Quote request", f"{banner}{_brief(db, job, rfq)}{ask}{_actions(token)}{foot}")


def _field(label: str, name: str, value: str = "", *, textarea: bool = False, placeholder: str = "", mode: str = "") -> str:
    attrs = f' inputmode="{mode}"' if mode else ""
    if textarea:
        control = f'<textarea id="{name}" name="{name}" rows="3" maxlength="{MAX_TEXT}" placeholder="{e(placeholder)}">{e(value)}</textarea>'
    else:
        control = f'<input id="{name}" name="{name}" type="text" maxlength="60" value="{e(value)}" placeholder="{e(placeholder)}"{attrs}>'
    return f'<div class="f"><label for="{name}">{e(label)}</label>{control}</div>'


def _back(token: str) -> str:
    return f"<p><a class='back' href='/q/{token}'>&larr; Back</a></p>"


def _quote_form(token: str, values: dict, error: str = "") -> str:
    kind = values.get("price_type", "fixed")
    radios = "".join(
        f'<label class="radio"><input type="radio" name="price_type" value="{k}"{" checked" if k == kind else ""}> {e(v)}</label>'
        for k, v in PRICE_TYPES.items()
    )
    err = f"<p class='error' role='alert'>{e(error)}</p>" if error else ""
    return (
        f"{_back(token)}<h2>Your quote</h2>{err}<form method='post' action='/q/{token}/quote'>"
        f'<div class="f"><span class="label">Price type</span><div class="radios">{radios}</div></div>'
        + _field("Price ($)", "price", values.get("price", ""), placeholder="e.g. 4,800", mode="decimal")
        + _field("Upper price, only for a range ($)", "price_max", values.get("price_max", ""), placeholder="e.g. 5,600", mode="decimal")
        + _field("What's included (one item per line)", "included", values.get("included", ""), textarea=True)
        + _field("What's not included", "excluded", values.get("excluded", ""), textarea=True)
        + _field("Timeline", "timeline", values.get("timeline", ""), placeholder="e.g. 3 days, can start in 2 weeks")
        + _field("Warranty", "warranty", values.get("warranty", ""), placeholder="e.g. 7 years on workmanship")
        + _field("Anything else (optional)", "notes", values.get("notes", ""), textarea=True)
        + "<button class='btn primary' type='submit'>Send quote</button></form>"
    )


def _slot_row(i: int, values: dict, today: str) -> str:
    return (
        f'<div class="slot"><span class="label">Option {i}{" (optional)" if i > 1 else ""}</span>'
        f'<input type="date" name="date{i}" value="{e(values.get(f"date{i}", ""))}" min="{today}" aria-label="Date for option {i}">'
        f'<div class="times"><input type="time" name="from{i}" value="{e(values.get(f"from{i}", ""))}" aria-label="From">'
        f'<span>to</span><input type="time" name="to{i}" value="{e(values.get(f"to{i}", ""))}" aria-label="To"></div></div>'
    )


def _visit_form(token: str, values: dict, error: str = "") -> str:
    err = f"<p class='error' role='alert'>{e(error)}</p>" if error else ""
    today = timeutil.local_now().strftime("%Y-%m-%d")
    return (
        f"{_back(token)}<h2>Arrange a site visit</h2><p class='muted'>Offer up to three times (a window is fine, such as 10am to "
        "12pm). The customer picks one, and shares their contact details with you only once they've confirmed.</p>"
        f"{err}<form method='post' action='/q/{token}/visit'>"
        + "".join(_slot_row(i, values, today) for i in (1, 2, 3))
        + _field("Ballpark price, if you can (optional, $)", "price", values.get("price", ""), placeholder="e.g. 4,000 to 6,000")
        + _field("Anything the customer should prepare (optional)", "notes", values.get("notes", ""), textarea=True)
        + "<button class='btn primary' type='submit'>Offer these times</button></form>"
    )


def parse_slots(form: dict) -> tuple[list[dict], str]:
    """Up to three (date, from, to) rows -> [{start, end, label}] in local time, or ([], message) if something is wrong."""
    now = timeutil.local_now()
    windows: list[dict] = []
    seen: set = set()
    for i in (1, 2, 3):
        day, begin, finish = ((form.get(f"{k}{i}") or "").strip() for k in ("date", "from", "to"))
        if not (day or begin or finish):
            continue
        if not (day and begin and finish):
            return [], f"Option {i} needs a date and both times."
        try:
            start = datetime.datetime.strptime(f"{day}T{begin[:5]}", "%Y-%m-%dT%H:%M")
            end = datetime.datetime.strptime(f"{day}T{finish[:5]}", "%Y-%m-%dT%H:%M")
        except ValueError:
            return [], f"Option {i} doesn't look like a valid date and time."
        if end <= start:
            return [], f"Option {i}: the end time must be after the start time."
        if end - start > datetime.timedelta(hours=12):
            return [], f"Option {i}: a window can be at most 12 hours."
        if start < now:
            return [], f"Option {i} is in the past."
        if start > now + datetime.timedelta(days=90):
            return [], f"Option {i} is more than 90 days away."
        if (start, end) not in seen:
            seen.add((start, end))
            windows.append({"start": timeutil.iso(start), "end": timeutil.iso(end), "label": timeutil.label(start, end)})
    if not windows:
        return [], "Please offer at least one time you could visit."
    return windows, ""


def _decline_form(token: str, values: dict, error: str = "") -> str:
    err = f"<p class='error' role='alert'>{e(error)}</p>" if error else ""
    chosen = values.get("reason", "")
    radios = "".join(
        f'<label class="radio"><input type="radio" name="reason" value="{k}"{" checked" if k == chosen else ""}> {e(v)}</label>'
        for k, v in DECLINE_REASONS.items()
    )
    return (
        f"{_back(token)}<h2>Can't help with this one</h2><p class='muted'>Thanks for letting us know. It stops us asking "
        f"again for similar jobs.</p>{err}<form method='post' action='/q/{token}/decline'>"
        f'<div class="f"><div class="radios">{radios}</div></div>'
        + _field("Note (optional)", "notes", values.get("notes", ""), textarea=True)
        + "<button class='btn primary' type='submit'>Send</button></form>"
    )


# --- Routes ---------------------------------------------------------------------------------------------
def _gate(request: Request, token: str, submitting: bool = False):
    """Rate-limit, then load the request. Returns (db, (rfq, job, company)) or an HTMLResponse to return."""
    ip = client_ip(request)
    if not view_limiter.allow(ip) or (submitting and (not submit_limiter_ip.allow(ip) or not submit_limiter_token.allow(token))):
        return _page("Slow down", "<h2>Too many requests</h2><p class='muted'>Please wait a few minutes and try again.</p>", 429)
    db = get_session()
    loaded = _load(db, token)
    if loaded is None:
        db.close()
        return _not_found()
    return db, loaded


@router.get("/q/{token}", response_class=HTMLResponse)
def landing(request: Request, token: str, saved: int = 0):
    gate = _gate(request, token)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (rfq, job, company) = gate
    try:
        return _landing(db, token, rfq, job, company, bool(saved))
    finally:
        db.close()


@router.get("/q/{token}/quote", response_class=HTMLResponse)
def quote_form(request: Request, token: str):
    gate = _gate(request, token)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (rfq, job, company) = gate
    try:
        existing = _existing(db, rfq)
        values: dict = {}
        if existing and existing.kind == "quote":
            values = {"price_type": existing.price_type, "price": f"{existing.price_amount:g}" if existing.price_amount else "",
                      "price_max": f"{existing.price_max:g}" if existing.price_max else "",
                      "included": "\n".join(existing.scope_included or []), "excluded": "\n".join(existing.exclusions or []),
                      "timeline": existing.timeline_text or "", "warranty": existing.warranty_notes or ""}
        return _page("Your quote", _quote_form(token, values))
    finally:
        db.close()


@router.post("/q/{token}/quote", response_class=HTMLResponse)
def quote_submit(request: Request, token: str, price_type: str = Form("fixed"), price: str = Form(""), price_max: str = Form(""),
                 included: str = Form(""), excluded: str = Form(""), timeline: str = Form(""), warranty: str = Form(""),
                 notes: str = Form("")):
    gate = _gate(request, token, submitting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (rfq, job, company) = gate
    try:
        values = {"price_type": price_type if price_type in PRICE_TYPES else "fixed", "price": price, "price_max": price_max,
                  "included": included, "excluded": excluded, "timeline": timeline, "warranty": warranty, "notes": notes}
        amount, high = parse_money(price), parse_money(price_max)
        if amount is None:
            return _page("Your quote", _quote_form(token, values, "Please enter your price as a number, for example 4800."), 400)
        if values["price_type"] == "range" and (high is None or high < amount):
            return _page("Your quote", _quote_form(token, values, "For a range, enter an upper price that is higher than the first price."), 400)
        save_response(db, rfq, job, company, kind="quote", price_type=values["price_type"], price=amount, price_max=high,
                      included=clean_text(included), excluded=clean_text(excluded), timeline=clean_text(timeline, 200),
                      warranty=clean_text(warranty, 200), notes=clean_text(notes))
        return RedirectResponse(f"/q/{token}?saved=1", status_code=303)
    finally:
        db.close()


@router.get("/q/{token}/visit", response_class=HTMLResponse)
def visit_form(request: Request, token: str):
    gate = _gate(request, token)
    if isinstance(gate, HTMLResponse):
        return gate
    db, _ = gate
    db.close()
    return _page("Arrange a site visit", _visit_form(token, {}))


@router.post("/q/{token}/visit", response_class=HTMLResponse)
def visit_submit(request: Request, token: str, date1: str = Form(""), from1: str = Form(""), to1: str = Form(""),
                 date2: str = Form(""), from2: str = Form(""), to2: str = Form(""), date3: str = Form(""), from3: str = Form(""),
                 to3: str = Form(""), price: str = Form(""), notes: str = Form("")):
    gate = _gate(request, token, submitting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (rfq, job, company) = gate
    try:
        values = {"date1": date1, "from1": from1, "to1": to1, "date2": date2, "from2": from2, "to2": to2, "date3": date3,
                  "from3": from3, "to3": to3, "price": price, "notes": notes}
        windows, error = parse_slots(values)
        if error:
            return _page("Arrange a site visit", _visit_form(token, values, error), 400)
        ballpark = clean_text(price, 60)
        save_response(db, rfq, job, company, kind="visit", visit_windows=windows,
                      notes=(f"Ballpark: {ballpark}. " if ballpark else "") + clean_text(notes))
        return RedirectResponse(f"/q/{token}?saved=1", status_code=303)
    finally:
        db.close()


@router.get("/q/{token}/visit.ics")
def visit_ics(request: Request, token: str):
    """Calendar file for the confirmed visit, for the supplier (the link in their email is their only credential)."""
    gate = _gate(request, token)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (rfq, job, company) = gate
    try:
        visit = db.query(Visit).filter_by(rfq_send_id=rfq.id, status="confirmed").order_by(Visit.created_at.desc()).first()
        if visit is None:
            return _not_found()
        customer = db.get(User, job.user_id) if job.user_id else None
        name, mime, data = visit_invite(visit, job, company, for_supplier=True, customer_first_name=customer.name if customer else None)
        return Response(data, media_type="text/calendar", headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})
    finally:
        db.close()


@router.get("/q/{token}/decline", response_class=HTMLResponse)
def decline_form(request: Request, token: str):
    gate = _gate(request, token)
    if isinstance(gate, HTMLResponse):
        return gate
    db, _ = gate
    db.close()
    return _page("Can't help", _decline_form(token, {}))


@router.post("/q/{token}/decline", response_class=HTMLResponse)
def decline_submit(request: Request, token: str, reason: str = Form(""), notes: str = Form("")):
    gate = _gate(request, token, submitting=True)
    if isinstance(gate, HTMLResponse):
        return gate
    db, (rfq, job, company) = gate
    try:
        if reason not in DECLINE_REASONS:
            return _page("Can't help", _decline_form(token, {"notes": notes}, "Please choose a reason."), 400)
        save_response(db, rfq, job, company, kind="decline", decline_reason=reason, notes=clean_text(notes))
        return RedirectResponse(f"/q/{token}?saved=1", status_code=303)
    finally:
        db.close()
