from __future__ import annotations

import base64
import datetime
import mimetypes
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from bs4 import BeautifulSoup
from googleapiclient.discovery import build

from services.attachments import display_name  # noqa: F401 (also imported from here by tests)
from services.google_oauth import get_credentials


class GmailNotConnected(RuntimeError):
    pass


def _service():
    creds = get_credentials()
    if not creds or not creds.valid:
        raise GmailNotConnected("Gmail is not connected. Go to Settings and click 'Connect Gmail'.")
    return build("gmail", "v1", credentials=creds)


def _attach_file(message: MIMEMultipart, path: Path, photo_number: int = 1) -> None:
    mime_type, _ = mimetypes.guess_type(path.name)
    maintype, _, subtype = (mime_type or "application/octet-stream").partition("/")
    data = path.read_bytes()
    name = display_name(path, photo_number)
    if maintype == "image":
        part = MIMEImage(data, _subtype=subtype, name=name)
    else:
        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=name)
    message.attach(part)


def build_mime_message(to: str, subject: str, body: str, attachment_paths: list[str] | None = None,
                       extra_files: list[tuple[str, str, bytes]] | None = None):
    """`extra_files` are (filename, mime type, bytes) attachments built in memory, such as a calendar invite."""
    valid_attachments = [Path(p) for p in (attachment_paths or []) if Path(p).exists()]
    if valid_attachments or extra_files:
        message = MIMEMultipart()
        message.attach(MIMEText(body, _charset="utf-8"))
        photo_number = 0
        for path in valid_attachments:
            if path.suffix.lower() != ".pdf":
                photo_number += 1
            _attach_file(message, path, photo_number)
        for filename, mime_type, data in extra_files or []:
            maintype, _, subtype = mime_type.partition("/")
            part = MIMEBase(maintype, subtype)
            part.set_payload(data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            message.attach(part)
    else:
        message = MIMEText(body, _charset="utf-8")

    message["to"] = to
    message["subject"] = Header(subject, "utf-8")
    return message


def send_email(to: str, subject: str, body: str, attachment_paths: list[str] | None = None,
               extra_files: list[tuple[str, str, bytes]] | None = None) -> tuple[str, str]:
    """Sends a plain-text email, optionally with file attachments. Returns (message_id, thread_id)."""
    service = _service()
    message = build_mime_message(to, subject, body, attachment_paths, extra_files)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return sent["id"], sent["threadId"]


def get_thread_messages(thread_id: str) -> list[dict]:
    service = _service()
    thread = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    return thread.get("messages", [])


def search_messages(query: str) -> list[dict]:
    service = _service()
    result = service.users().messages().list(userId="me", q=query).execute()
    ids = [m["id"] for m in result.get("messages", [])]
    return [service.users().messages().get(userId="me", id=mid, format="full").execute() for mid in ids]


def _header(message: dict, name: str) -> str | None:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode() + b"==").decode("utf-8", errors="ignore")


def extract_body_text(message: dict) -> str:
    """Prefers text/plain; falls back to stripping text/html."""
    payload = message.get("payload", {})

    def walk(part: dict) -> tuple[str | None, str | None]:
        mime = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")
        plain, html = None, None
        if mime == "text/plain" and body_data:
            plain = _decode_part(body_data)
        elif mime == "text/html" and body_data:
            html = _decode_part(body_data)
        for sub in part.get("parts", []) or []:
            sub_plain, sub_html = walk(sub)
            plain = plain or sub_plain
            html = html or sub_html
        return plain, html

    plain, html = walk(payload)
    if plain:
        return plain.strip()
    if html:
        return BeautifulSoup(html, "html.parser").get_text("\n").strip()
    return ""


def message_to_reply_dict(message: dict) -> dict:
    from_header = _header(message, "From") or ""
    from_email = from_header.split("<")[-1].rstrip(">") if "<" in from_header else from_header
    internal_date_ms = int(message.get("internalDate", "0"))
    received_at = datetime.datetime.utcfromtimestamp(internal_date_ms / 1000) if internal_date_ms else None
    return {
        "gmail_message_id": message["id"],
        "gmail_thread_id": message.get("threadId"),
        "from_email": from_email,
        "subject": _header(message, "Subject"),
        "received_at": received_at,
        "raw_body_text": extract_body_text(message),
    }
