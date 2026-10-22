from __future__ import annotations

import logging
import mimetypes
import uuid
from pathlib import Path

import httpx
from twilio.request_validator import RequestValidator
from twilio.rest import Client

import config
from db.models import User, WaMessage
from db.session import get_session

log = logging.getLogger("whatsapp")

ALLOWED_MEDIA_PREFIXES = ("image/",)
MAX_MEDIA_BYTES = 10_000_000


def webhook_url(path: str) -> str:
    """The exact public URL Twilio signed. Behind a proxy the request URL differs, so build it from config."""
    return f"{config.PUBLIC_BASE_URL}{path}"


def signature_is_valid(url: str, form: dict[str, str], signature: str | None) -> bool:
    if not config.TWILIO_AUTH_TOKEN or not signature:
        return False
    return RequestValidator(config.TWILIO_AUTH_TOKEN).validate(url, form, signature)


def number_allowed(whatsapp_number: str) -> bool:
    """Beta gate. If ALLOWED_NUMBERS is empty anyone may use the bot."""
    if not config.ALLOWED_NUMBERS:
        return True
    normalised = {n.replace("whatsapp:", "").strip() for n in config.ALLOWED_NUMBERS}
    return whatsapp_number.replace("whatsapp:", "").strip() in normalised


def send_whatsapp(user: User, body: str) -> WaMessage:
    """Send (or, in dry_run, just record) a WhatsApp message to a user. Always logs a WaMessage."""
    session = get_session()
    try:
        record = WaMessage(user_id=user.id, direction="out", body=body)
        live = (
            config.OUTBOUND_MODE == "live"
            and config.TWILIO_ACCOUNT_SID
            and config.TWILIO_AUTH_TOKEN
            and config.TWILIO_WHATSAPP_FROM
        )
        if live:
            try:
                client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
                msg = client.messages.create(from_=config.TWILIO_WHATSAPP_FROM, to=user.whatsapp_number, body=body)
                record.twilio_sid = msg.sid
                record.delivery = "sent"
            except Exception as exc:  # noqa: BLE001
                log.exception("WhatsApp send failed")
                record.delivery = f"failed: {exc}"[:200]
        else:
            record.delivery = "dry_run"
            log.info("[dry_run] -> %s: %s", user.whatsapp_number, body)
        session.add(record)
        session.commit()
        return record
    finally:
        session.close()


def download_media(url: str, content_type: str, dest_dir: Path) -> Path | None:
    """Download one Twilio media item. Images only, size-capped. Returns the saved path or None."""
    if not content_type.startswith(ALLOWED_MEDIA_PREFIXES):
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    auth = (config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN) if config.TWILIO_ACCOUNT_SID else None
    try:
        with httpx.Client(follow_redirects=True, timeout=20) as client:
            resp = client.get(url, auth=auth)
            resp.raise_for_status()
            if len(resp.content) > MAX_MEDIA_BYTES:
                log.warning("Media too large, skipped (%d bytes)", len(resp.content))
                return None
            ext = mimetypes.guess_extension(content_type.split(";")[0]) or ".jpg"
            if ext == ".jpe":
                ext = ".jpg"
            path = dest_dir / f"{uuid.uuid4().hex[:10]}{ext}"
            path.write_bytes(resp.content)
            return path
    except Exception:  # noqa: BLE001
        log.exception("Media download failed")
        return None
