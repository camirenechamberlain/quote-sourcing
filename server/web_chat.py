"""Website chat: the customer-facing front door. Wraps the intake and supplier-review agents."""
from __future__ import annotations

import datetime
import hashlib
import html
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

import phonenumbers
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import config
from db.models import ChatSession, Company, IntakeSession, Job, Quote, ReplyEmail, RfqSend, User, WaMessage
from db.session import get_session
from server import assets
from server.agent import intake
from services import attachments as attachments_service
from services import documents, images

log = logging.getLogger("web_chat")
router = APIRouter()

MAX_TEXT_CHARS = 2000
MAX_FILES_PER_MESSAGE = 6
MAX_ATTACHMENTS_PER_REQUEST = 12
MAX_PDFS_PER_REQUEST = 4
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# --- Small in-memory rate limiter (single process; fine for the MVP) ---------------------------------
class SlidingWindow:
    def __init__(self, limit: int, seconds: int):
        self.limit, self.seconds = limit, seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.seconds:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


start_limiter = SlidingWindow(limit=10, seconds=600)
message_limiter_ip = SlidingWindow(limit=40, seconds=60)
message_limiter_token = SlidingWindow(limit=20, seconds=60)


# --- Helpers -------------------------------------------------------------------------------------------
def client_ip(request: Request) -> str:
    if config.env("TRUST_PROXY_HEADERS") == "1":  # only enable when the app sits behind Cloudflare / a proxy
        fwd = request.headers.get("CF-Connecting-IP") or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
        if fwd:
            return fwd
    return request.client.host if request.client else "unknown"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_mobile(raw: str) -> str:
    try:
        parsed = phonenumbers.parse((raw or "").strip(), "AU")
    except phonenumbers.NumberParseException as exc:
        raise ValueError("That doesn't look like a valid mobile number.") from exc
    kind = phonenumbers.number_type(parsed)
    ok_types = (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)
    if not phonenumbers.is_valid_number(parsed) or kind not in ok_types:
        raise ValueError("Please enter a valid mobile number (for example 0412 345 678).")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def _since() -> datetime.datetime:
    return datetime.datetime.utcnow() - datetime.timedelta(hours=24)


def jobs_for_mobile_24h(db, mobile: str) -> int:
    return (
        db.query(Job)
        .join(User, Job.user_id == User.id)
        .filter(User.mobile == mobile, Job.created_at >= _since())
        .count()
    )


def jobs_global_24h(db) -> int:
    return db.query(Job).filter(Job.user_id.isnot(None), Job.created_at >= _since()).count()


def web_messages_global_24h(db) -> int:
    """Inbound website chat messages in the last 24h (all customers, including those who haven't given contact details)."""
    return (
        db.query(WaMessage)
        .join(User, WaMessage.user_id == User.id)
        .filter(User.whatsapp_number.like("web:%"), WaMessage.direction == "in", WaMessage.created_at >= _since())
        .count()
    )


def clean_first_name(raw: str) -> str:
    match = re.match(r"\s*([A-Za-z][A-Za-z'\-]{0,39})", raw or "")
    if not match:
        raise ValueError("Please enter your first name.")
    return match.group(1).title()


def validate_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise ValueError("Please enter a valid email address.")
    return email


def limit_reached_message() -> str:
    return (
        f"You've reached today's limit of {config.DAILY_QUOTE_LIMIT} quote requests for this number. "
        "Please come back tomorrow. (Higher limits are coming.)"
    )


BUSY_MESSAGE = "We're at capacity for today. Please try again tomorrow."


def public_base(request: Request) -> str:
    return config.PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def _load_chat(db, token: str) -> tuple[ChatSession, User]:
    if not token or len(token) > 200:
        raise HTTPException(status_code=401, detail="invalid session")
    chat = db.query(ChatSession).filter_by(token_hash=hash_token(token)).first()
    if chat is None:
        raise HTTPException(status_code=401, detail="invalid session")
    return chat, db.get(User, chat.user_id)


def _state(db, user: User, request: Request) -> dict:
    session = (
        db.query(IntakeSession).filter_by(user_id=user.id).order_by(IntakeSession.created_at.desc()).first()
    )
    job = None
    state = "idle"
    if session:
        state = session.state
        if session.state == "confirmed" and session.job_id:
            j = db.get(Job, session.job_id)
            job = {"ref": j.ref_prefix, "track_url": f"{public_base(request)}/j/{j.public_token}"}
    return {
        "state": state,
        "job": job,
        "needs_details": not user.mobile,
        # True while the supplier review has asked questions and the customer may still answer or "send as is"
        "review_pending": intake.review_pending(session),
        # The open review question's tappable answers (None when no question is open); the page renders them as buttons
        "choices": intake.current_choices(session) if session else None,
    }


def _log_message(db, user: User, direction: str, body: str, media: list[str] | None = None) -> None:
    db.add(WaMessage(user_id=user.id, direction=direction, body=body, media_paths=media or [], delivery="web"))


# --- API -----------------------------------------------------------------------------------------------
@router.post("/api/chat/start")
def start_chat(request: Request):
    """Open an anonymous chat. Contact details are collected later, once the request has been refined."""
    ip = client_ip(request)
    if not start_limiter.allow(ip):
        return JSONResponse({"error": "slow_down", "message": "Too many attempts. Please wait a few minutes."}, status_code=429)

    db = get_session()
    try:
        if db.query(ChatSession).filter(ChatSession.ip == ip, ChatSession.created_at >= _since()).count() >= config.IP_DAILY_SESSIONS:
            return JSONResponse({"error": "slow_down", "message": "Too many chats from this network today. Please try tomorrow."}, status_code=429)
        if web_messages_global_24h(db) >= config.GLOBAL_DAILY_MESSAGES or jobs_global_24h(db) >= config.GLOBAL_DAILY_JOBS:
            return JSONResponse({"error": "busy", "message": BUSY_MESSAGE}, status_code=503)

        token = secrets.token_urlsafe(32)
        user = User(whatsapp_number=f"web:{secrets.token_hex(8)}")
        db.add(user)
        db.flush()
        db.add(ChatSession(token_hash=hash_token(token), user_id=user.id, ip=ip))
        db.commit()
        return {"token": token, "messages": [], "state": "idle", "job": None, "needs_details": True}
    finally:
        db.close()


class ContactBody(BaseModel):
    token: str
    name: str
    mobile: str
    email: str
    consent: bool = False


@router.post("/api/chat/contact")
def submit_contact(body: ContactBody, request: Request):
    """Final step: collect who the customer is, enforce the limits, then confirm the job."""
    ip = client_ip(request)
    if not message_limiter_ip.allow(ip) or not message_limiter_token.allow(hash_token(body.token)):
        return JSONResponse({"error": "slow_down", "message": "Too many attempts. Give it a moment."}, status_code=429)
    if not body.consent:
        return JSONResponse({"error": "consent", "message": "Please tick the box to continue."}, status_code=400)
    try:
        name = clean_first_name(body.name)
    except ValueError as exc:
        return JSONResponse({"error": "name", "message": str(exc)}, status_code=400)
    try:
        mobile = normalise_mobile(body.mobile)
    except ValueError as exc:
        return JSONResponse({"error": "mobile", "message": str(exc)}, status_code=400)
    try:
        email = validate_email(body.email)
    except ValueError as exc:
        return JSONResponse({"error": "email", "message": str(exc)}, status_code=400)

    db = get_session()
    try:
        chat, user = _load_chat(db, body.token)
        session = (
            db.query(IntakeSession)
            .filter(IntakeSession.user_id == user.id, IntakeSession.state == "awaiting_confirm")
            .first()
        )
        if session is None:
            return JSONResponse({"error": "not_ready", "message": "Let's finish describing the job first."}, status_code=400)

        # Limits are checked here, at the moment the request would become a job.
        if jobs_for_mobile_24h(db, mobile) >= config.DAILY_QUOTE_LIMIT:
            session.state = "cancelled"
            msg = limit_reached_message()
            _log_message(db, user, "out", msg)
            db.commit()
            return JSONResponse({"error": "limit_reached", "message": msg, "replies": [msg], "state": "limit_reached", "job": None}, status_code=429)
        if jobs_global_24h(db) >= config.GLOBAL_DAILY_JOBS:
            return JSONResponse({"error": "busy", "message": BUSY_MESSAGE}, status_code=503)

        user.name, user.mobile, user.email = name, mobile, email

        def confirm_text(job: Job) -> str:
            return (
                f"Done, {name}! Your reference is {job.ref_prefix}. I'm lining up suppliers for you now. "
                f"Bookmark this link to follow progress: {public_base(request)}/j/{job.public_token}"
            )

        replies = intake.handle_message(db, user, "YES", [], confirm_message=confirm_text)
        for r in replies:
            _log_message(db, user, "out", r)
        db.commit()
        return {"replies": replies, **_state(db, user, request)}
    finally:
        db.close()


@router.post("/api/chat/message")
def chat_message(
    request: Request,
    token: str = Form(...),
    text: str = Form(""),
    files: list[UploadFile] = File(default=[]),
):
    ip = client_ip(request)
    if not message_limiter_ip.allow(ip) or not message_limiter_token.allow(hash_token(token)):
        return JSONResponse({"error": "slow_down", "message": "You're sending messages very fast. Give it a moment."}, status_code=429)

    text = (text or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        return JSONResponse({"error": "too_long", "message": f"Please keep messages under {MAX_TEXT_CHARS} characters."}, status_code=400)
    if len(files) > MAX_FILES_PER_MESSAGE:
        return JSONResponse({"error": "too_many_files", "message": f"You can attach up to {MAX_FILES_PER_MESSAGE} files at a time."}, status_code=400)
    if not text and not files:
        return JSONResponse({"error": "empty", "message": "Type a message or attach a photo or PDF."}, status_code=400)

    db = get_session()
    try:
        chat, user = _load_chat(db, token)
        if web_messages_global_24h(db) >= config.GLOBAL_DAILY_MESSAGES:
            return JSONResponse({"error": "busy", "message": BUSY_MESSAGE}, status_code=503)
        if chat.message_count >= config.MAX_MESSAGES_PER_SESSION:
            return JSONResponse({"error": "session_full", "message": "This chat has reached its length limit. Please start a new one."}, status_code=429)

        open_session = (
            db.query(IntakeSession)
            .filter(IntakeSession.user_id == user.id, IntakeSession.state.in_(["collecting", "awaiting_confirm"]))
            .first()
        )

        # "Send as is" (after the supplier review asked its questions) counts as a confirmation.
        if not files and intake.accepts_as_is(open_session, text):
            intake.accept_as_is(open_session)
            wants_to_confirm = True
        else:
            wants_to_confirm = (
                open_session is not None
                and open_session.state == "awaiting_confirm"
                and not files
                and intake.normalise_command(text) in intake.YES_WORDS
            )
        if wants_to_confirm and not user.mobile:
            _log_message(db, user, "in", text)
            # First read the request as a supplier would. If something obvious is missing, ask before asking for contact details.
            gaps = intake.run_supplier_review(open_session)
            if gaps:
                for g in gaps:
                    _log_message(db, user, "out", g)
                db.commit()
                return {"replies": gaps, **_state(db, user, request)}
            msg = "Almost there! I just need a few contact details so I can send your progress link and sign the requests with your name."
            _log_message(db, user, "out", msg)
            db.commit()
            return {"replies": [msg], **_state(db, user, request), "state": "need_details"}

        # Defensive: a chat that already has contact details is re-checked against the limits at confirmation.
        if (
            user.mobile
            and open_session
            and open_session.state == "awaiting_confirm"
            and intake.normalise_command(text) in intake.YES_WORDS
            and (jobs_for_mobile_24h(db, user.mobile) >= config.DAILY_QUOTE_LIMIT or jobs_global_24h(db) >= config.GLOBAL_DAILY_JOBS)
        ):
            reached = jobs_for_mobile_24h(db, user.mobile) >= config.DAILY_QUOTE_LIMIT
            open_session.state = "cancelled"
            msg = limit_reached_message() if reached else BUSY_MESSAGE
            _log_message(db, user, "in", text)
            _log_message(db, user, "out", msg)
            db.commit()
            return {"replies": [msg], **_state(db, user, request), "state": "limit_reached"}

        existing = list(open_session.photo_paths or []) if open_session else []
        if len(existing) + len(files) > MAX_ATTACHMENTS_PER_REQUEST:
            return JSONResponse({"error": "too_many_attachments", "message": f"A request can include up to {MAX_ATTACHMENTS_PER_REQUEST} attachments."}, status_code=400)
        pdfs_so_far = sum(1 for p in existing if p.lower().endswith(".pdf"))

        saved: list[str] = []

        def discard_saved() -> None:
            for p in saved:
                Path(p).unlink(missing_ok=True)

        dest = attachments_service.ATTACHMENTS_DIR / "_incoming" / user.id
        for f in files:
            raw = f.file.read(documents.MAX_PDF_BYTES + 1)
            try:
                if documents.is_pdf(raw):  # decided by content, never by the file name or claimed type
                    pdfs_so_far += 1
                    if pdfs_so_far > MAX_PDFS_PER_REQUEST:
                        discard_saved()
                        return JSONResponse({"error": "too_many_pdfs", "message": f"A request can include up to {MAX_PDFS_PER_REQUEST} PDFs."}, status_code=400)
                    path = documents.save_sanitised_pdf(raw, dest, secrets.token_hex(4), f.filename)
                else:
                    path = images.save_sanitised(raw, dest, secrets.token_hex(6))
            except (images.BadImage, documents.BadPdf) as exc:
                discard_saved()
                return JSONResponse({"error": "bad_upload", "message": f"{f.filename or 'A file'}: {exc}"}, status_code=400)
            saved.append(str(path))

        # Everything is emailed to suppliers, so keep the total under the soft cap (Gmail's limit is ~25MB).
        if attachments_service.total_size(existing + saved) > attachments_service.MAX_TOTAL_BYTES:
            discard_saved()
            limit_mb = attachments_service.MAX_TOTAL_BYTES // 1_000_000
            return JSONResponse({"error": "too_large", "message": f"Attachments can total up to {limit_mb}MB. Try removing a file or sending a smaller PDF."}, status_code=400)

        chat.message_count += 1
        _log_message(db, user, "in", text, saved)
        db.flush()

        def confirm_text(job: Job) -> str:
            return (
                f"Done! Your reference is {job.ref_prefix}. I'm lining up suppliers for you now. "
                f"Bookmark this link to follow progress: {public_base(request)}/j/{job.public_token}"
            )

        replies = intake.handle_message(db, user, text, saved, confirm_message=confirm_text)
        for r in replies:
            _log_message(db, user, "out", r)
        db.commit()
        return {"replies": replies, **_state(db, user, request)}
    finally:
        db.close()


@router.get("/api/chat/state")
def chat_state(request: Request, token: str):
    db = get_session()
    try:
        chat, user = _load_chat(db, token)
        rows = db.query(WaMessage).filter_by(user_id=user.id).order_by(WaMessage.created_at, WaMessage.id).all()
        messages = [
            {"role": "user" if m.direction == "in" else "bot", "text": m.body, "photos": len(m.media_paths or [])}
            for m in rows
        ]
        return {"messages": messages, **_state(db, user, request)}
    finally:
        db.close()


# --- Customer progress page ------------------------------------------------------------------------------
def _supplier_status(db, job: Job, company: Company) -> str:
    rfq = db.query(RfqSend).filter_by(job_id=job.id, company_id=company.id, status="sent").first()
    if rfq is None:
        return "Queued"
    reply = db.query(ReplyEmail).filter_by(rfq_send_id=rfq.id).first()
    if reply is None:
        return "Request sent"
    quote = db.query(Quote).filter_by(reply_email_id=reply.id).first()
    return "Quote received" if quote and quote.is_quote else "Replied"


@router.get("/j/{public_token}", response_class=HTMLResponse)
def progress_page(public_token: str):
    db = get_session()
    try:
        job = db.query(Job).filter_by(public_token=public_token).first()
        if job is None:
            return HTMLResponse("<h1>Not found</h1>", status_code=404)
        companies = db.query(Company).filter_by(job_id=job.id, included=True).order_by(Company.name).all()
        rows = [(c.name, _supplier_status(db, job, c)) for c in companies]
        sent = sum(1 for _, s in rows if s != "Queued")
        quotes = sum(1 for _, s in rows if s == "Quote received")
        e = html.escape
        if rows:
            table = "".join(f"<tr><td>{e(n)}</td><td><span class='chip'>{e(s)}</span></td></tr>" for n, s in rows)
            body = (
                f"<p class='muted'>{sent} of {len(rows)} requests sent &middot; {quotes} quote(s) received</p>"
                f"<table><thead><tr><th>Supplier</th><th>Status</th></tr></thead><tbody>{table}</tbody></table>"
            )
        else:
            body = "<p>We're finding suppliers for you. This page refreshes automatically.</p>"
        page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="60">
<meta name="robots" content="noindex"><title>Your quote request</title>
<link rel="stylesheet" href="{assets.asset('chat.css')}"></head><body class="page">
<main class="card"><h1 class="brand"><span class="b1">quote</span><span class="b2">schmote</span></h1>
<h2>{e(job.title)}</h2><p class="muted">{e(job.category)} &middot; {e(job.location_text)} &middot; ref {e(job.ref_prefix)}</p>
{body}</main></body></html>"""
        return HTMLResponse(page)
    finally:
        db.close()
