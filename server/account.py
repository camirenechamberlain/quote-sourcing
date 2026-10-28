"""Customer accounts without passwords. A customer enters their email, we email a one-time sign-in link, and a cookie keeps them signed
in. "My requests" lists every request made with that email.

Safety notes:
  * The sign-in email is only sent if that address actually has requests, and the page always gives the same answer, so it can't be
    used to find out who has an account or to make us email strangers.
  * Links are single-use and expire in 15 minutes. Opening the link only shows a "Sign in" button: email scanners that pre-open links
    can't use it up, and only the real button press signs you in.
  * Only the hash of a link or cookie is stored. The cookie is HttpOnly, SameSite=Lax, and Secure when the site address is https.
"""
from __future__ import annotations

import datetime
import html
import re
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func

import config
from db.models import AccountLoginToken, AccountSession, Job, User
from db.session import get_session
from server.supplier_response import _page
from server.web_chat import SlidingWindow, client_ip, hash_token, public_base
from services import timeutil
from services.job_state import summarise
from services.notify import send_notification

router = APIRouter()

COOKIE = "qs_account"
LOGIN_TTL = datetime.timedelta(minutes=15)
SESSION_TTL = datetime.timedelta(days=30)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")

login_ip_limiter = SlidingWindow(limit=8, seconds=600)
login_email_limiter = SlidingWindow(limit=3, seconds=3600)
verify_limiter = SlidingWindow(limit=30, seconds=600)
e = html.escape


def normalise_email(raw: str | None) -> str | None:
    value = (raw or "").strip().lower()
    return value if EMAIL_RE.match(value) and len(value) <= 254 else None


def jobs_for(db, email: str) -> list[Job]:
    """Every request made with this email, newest first. (Each chat is its own user row, so match on the email.)"""
    return (db.query(Job).join(User, Job.user_id == User.id).filter(func.lower(User.email) == email)
            .order_by(Job.created_at.desc()).all())


def current_email(request: Request, db) -> str | None:
    raw = request.cookies.get(COOKIE)
    if not raw or len(raw) > 200:
        return None
    row = db.query(AccountSession).filter_by(token_hash=hash_token(raw)).first()
    if row is None or row.expires_at < datetime.datetime.utcnow():
        return None
    return row.email


def _secure() -> bool:
    return config.PUBLIC_BASE_URL.startswith("https://")


def _login_form(message: str = "", error: str = "") -> HTMLResponse:
    note = f"<div class='notice'>{e(message)}</div>" if message else ""
    err = f"<p class='error' role='alert'>{e(error)}</p>" if error else ""
    return _page("Your requests", f"""<p><a class='back' href='/'>&larr; Back to Quoteschmote</a></p><h2>Your requests</h2>
<p class='muted'>Enter the email you used when you made a request, and we'll email you a link to sign in. No password needed.</p>{note}{err}
<form method='post' action='/account/login'>
<div class='f'><label for='email'>Email</label><input id='email' name='email' type='email' autocomplete='email' maxlength='254' placeholder='you@example.com'></div>
<button class='btn primary' type='submit'>Email me a sign-in link</button></form>""")


def _job_card(db, job: Job) -> str:
    s = summarise(db, job)
    when = timeutil.utc_to_local(job.created_at).strftime("%d %b").lstrip("0")
    detail = []
    if s.contacted:
        detail.append(f"{s.contacted} supplier{'s' if s.contacted != 1 else ''} contacted")
    if s.declined:
        detail.append(f"{s.declined} declined")
    return (f"<a class='job' href='/j/{e(job.public_token)}'><b>{e(job.title)}</b>"
            f"<span class='muted'>{e(job.category)} &middot; {e(job.location_text)} &middot; {when} &middot; ref {e(job.ref_prefix)}</span>"
            f"<span class='chip'>{e(s.label)}</span>"
            + (f"<span class='detail'>{e(' · '.join(detail))}</span>" if detail else "") + "</a>")


def _dashboard(db, email: str) -> HTMLResponse:
    jobs = jobs_for(db, email)
    cards = "".join(_job_card(db, j) for j in jobs) or "<p class='muted'>No requests yet.</p>"
    return _page("Your requests", f"""<h2>Your requests</h2><p class='muted'>Signed in as {e(email)}</p>{cards}
<a class='btn primary' href='/'>Start a new request</a>
<form method='post' action='/account/logout'><button class='btn quiet' type='submit'>Sign out</button></form>""")


@router.get("/account", response_class=HTMLResponse)
def account(request: Request):
    db = get_session()
    try:
        email = current_email(request, db)
        return _dashboard(db, email) if email else _login_form()
    finally:
        db.close()


@router.post("/account/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form("")):
    address = normalise_email(email)
    if address is None:
        return _login_form(error="Please enter a valid email address.")
    ip = client_ip(request)
    if not login_ip_limiter.allow(ip):
        return _page("Slow down", "<h2>Too many attempts</h2><p class='muted'>Please wait a few minutes and try again.</p>", 429)
    same_answer = _login_form(message="If that email has any requests, we've sent a sign-in link to it. It works once and lasts 15 minutes.")
    if not login_email_limiter.allow(address):  # quietly stop repeat emails to one address, with the same answer as success
        return same_answer
    db = get_session()
    try:
        if not jobs_for(db, address):
            return same_answer
        raw = secrets.token_urlsafe(32)
        now = datetime.datetime.utcnow()
        db.add(AccountLoginToken(token_hash=hash_token(raw), email=address, expires_at=now + LOGIN_TTL))
        db.commit()
        link = f"{public_base(request)}/account/verify?token={raw}"
        send_notification(db, audience="customer", kind="login_link", to_email=address, subject="Your Quoteschmote sign-in link",
                          body=(f"Hi,\n\nUse this link to see your quote requests. It works once and expires in 15 minutes:\n\n{link}\n\n"
                                "If you didn't ask for this, you can ignore this email.\n\nQuoteschmote\n"))
        return same_answer
    finally:
        db.close()


def _valid_login_token(db, raw: str) -> AccountLoginToken | None:
    if not raw or len(raw) > 200:
        return None
    row = db.query(AccountLoginToken).filter_by(token_hash=hash_token(raw)).first()
    if row is None or row.used_at is not None or row.expires_at < datetime.datetime.utcnow():
        return None
    return row


@router.get("/account/verify", response_class=HTMLResponse)
def verify_page(request: Request, token: str = ""):
    if not verify_limiter.allow(client_ip(request)):
        return _page("Slow down", "<h2>Too many attempts</h2>", 429)
    db = get_session()
    try:
        if _valid_login_token(db, token) is None:
            return _page("Link expired", "<h2>That link has expired or was already used</h2><p class='muted'>Sign-in links work once and "
                         "last 15 minutes.</p><a class='btn primary' href='/account'>Get a new link</a>", 400)
        return _page("Sign in", f"<h2>Sign in to Quoteschmote</h2><form method='post' action='/account/verify'>"
                                f"<input type='hidden' name='token' value='{e(token)}'><button class='btn primary' type='submit'>Sign in</button></form>")
    finally:
        db.close()


@router.post("/account/verify")
def verify(request: Request, token: str = Form("")):
    if not verify_limiter.allow(client_ip(request)):
        return _page("Slow down", "<h2>Too many attempts</h2>", 429)
    db = get_session()
    try:
        row = _valid_login_token(db, token)
        if row is None:
            return _page("Link expired", "<h2>That link has expired or was already used</h2><a class='btn primary' href='/account'>Get a new link</a>", 400)
        now = datetime.datetime.utcnow()
        row.used_at = now
        raw = secrets.token_urlsafe(32)
        db.add(AccountSession(token_hash=hash_token(raw), email=row.email, expires_at=now + SESSION_TTL))
        db.commit()
        response = RedirectResponse("/account", status_code=303)
        response.set_cookie(COOKIE, raw, max_age=int(SESSION_TTL.total_seconds()), httponly=True, samesite="lax", secure=_secure(), path="/")
        return response
    finally:
        db.close()


@router.post("/account/logout")
def logout(request: Request):
    db = get_session()
    try:
        raw = request.cookies.get(COOKIE)
        if raw:
            db.query(AccountSession).filter_by(token_hash=hash_token(raw)).delete()
            db.commit()
        response = RedirectResponse("/account", status_code=303)
        response.delete_cookie(COOKIE, path="/")
        return response
    finally:
        db.close()
