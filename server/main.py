from __future__ import annotations

import logging
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

import config
from db.models import IntakeSession, User, WaMessage
from db.session import get_session
from server import assets, web_chat, whatsapp
from server.agent import intake
from services import attachments as attachments_service

log = logging.getLogger("server")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Quoteschmote", docs_url=None, redoc_url=None, openapi_url=None)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(web_chat.router)

WEBHOOK_PATH = "/whatsapp/webhook"
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

CSP = (
    "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"  # progress links carry a secret token in the path
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/static/"):
        # Versioned URLs can be cached forever (a change means a new URL); anything else must be revalidated.
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if "v" in request.query_params else "no-cache"
        )
    return response


@app.get("/")
def home() -> HTMLResponse:
    # The page itself is never cached; the assets it references carry a version token (see server/assets.py).
    return HTMLResponse(assets.versioned_html("chat.html"), headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "outbound_mode": config.OUTBOUND_MODE}


def _collect_media(form: dict[str, str], user_id: str, session_id: str) -> list[str]:
    saved: list[str] = []
    count = int(form.get("NumMedia", "0") or 0)
    dest = attachments_service.ATTACHMENTS_DIR / "_incoming" / user_id / session_id
    for i in range(count):
        url, ctype = form.get(f"MediaUrl{i}"), form.get(f"MediaContentType{i}", "")
        if not url:
            continue
        path = whatsapp.download_media(url, ctype, dest)
        if path:
            saved.append(str(path))
    return saved


def process_inbound(message_id: str, form: dict[str, str]) -> None:
    """Runs after the webhook has answered Twilio (so we never hit its response timeout)."""
    db = get_session()
    try:
        message = db.get(WaMessage, message_id)
        user = db.get(User, message.user_id)

        # Photos are attached to the user's open intake session (or a fresh one).
        session = (
            db.query(IntakeSession)
            .filter(IntakeSession.user_id == user.id, IntakeSession.state.in_(["collecting", "awaiting_confirm"]))
            .order_by(IntakeSession.created_at.desc())
            .first()
        )
        folder_key = session.id if session else message.id
        media_paths = _collect_media(form, user.id, folder_key)
        message.media_paths = media_paths

        replies = intake.handle_message(db, user, message.body, media_paths)
        db.commit()
    except Exception:  # noqa: BLE001
        log.exception("Failed processing inbound message %s", message_id)
        db.rollback()
        return
    finally:
        db.close()

    for reply in replies:
        whatsapp.send_whatsapp(user, reply)


@app.post(WEBHOOK_PATH)
async def whatsapp_webhook(request: Request, background: BackgroundTasks) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}

    if not whatsapp.signature_is_valid(
        whatsapp.webhook_url(WEBHOOK_PATH), form, request.headers.get("X-Twilio-Signature")
    ):
        raise HTTPException(status_code=403, detail="invalid signature")

    sender = form.get("From", "")
    sid = form.get("MessageSid", "")
    if not sender or not sid:
        raise HTTPException(status_code=400, detail="missing From/MessageSid")

    # Not on the beta list: acknowledge Twilio, do nothing, say nothing.
    if not whatsapp.number_allowed(sender):
        log.warning("Ignoring message from non-allowlisted number %s", sender)
        return Response(content=EMPTY_TWIML, media_type="application/xml")

    db = get_session()
    try:
        user = db.query(User).filter_by(whatsapp_number=sender).first()
        if user is None:
            user = User(whatsapp_number=sender)
            db.add(user)
            db.flush()
        message = WaMessage(user_id=user.id, direction="in", body=(form.get("Body") or "").strip(), twilio_sid=sid)
        db.add(message)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # Twilio retried a message we already have
            return Response(content=EMPTY_TWIML, media_type="application/xml")
        message_id = message.id
    finally:
        db.close()

    background.add_task(process_inbound, message_id, form)
    return Response(content=EMPTY_TWIML, media_type="application/xml")
