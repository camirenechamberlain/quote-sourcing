"""A styled HTML version of our plain-text emails, with response buttons.

The plain-text body stays the source of truth (it is what you review and edit in the admin app, and it is always sent as the fallback part
of the email). This turns it into HTML: paragraphs, tidy links, and, where the body carries a supplier's response link
(https://.../q/<token> on a line of its own), three big buttons that go straight to the right form instead of to a page of choices.

Buttons are plain links, deliberately. A link that changes anything on a single click (such as "Can't help") would be triggered by the
link scanners many mail systems run, and would wrongly record answers the supplier never gave. So every button opens a page where the
supplier confirms or fills in the details.
"""
from __future__ import annotations

import html
import re

_RESPONSE_LINE = re.compile(r"^(https?://\S+/q/[A-Za-z0-9_-]{16,64})$")
_URL = re.compile(r"(https?://[^\s<]+)")

INK, BLUE = "#0B0B0F", "#2D4BFF"
BUTTONS = (
    ("Send my quote", "/quote", True),
    ("I need to see the job first", "/visit", False),
    ("Can't help with this one", "/decline", False),
)


def _linkify(escaped: str) -> str:
    def link(match: re.Match) -> str:
        url = match.group(1)
        trailing = ""
        while url and url[-1] in ".,;:!?)":  # punctuation that follows a link in a sentence isn't part of it
            trailing, url = url[-1] + trailing, url[:-1]
        return f'<a href="{url}" style="color:{BLUE}">{url}</a>{trailing}'

    return _URL.sub(link, escaped)


def _button(label: str, url: str, primary: bool) -> str:
    style = (f"display:block;padding:14px 18px;border-radius:999px;text-align:center;text-decoration:none;font-weight:700;font-size:16px;"
             + (f"background:{BLUE};color:#ffffff;border:2px solid {BLUE};" if primary else f"background:#ffffff;color:{INK};border:2px solid {INK};"))
    return f'<tr><td style="padding:5px 0"><a href="{html.escape(url)}" style="{style}">{html.escape(label)}</a></td></tr>'


def _button_block(base_url: str) -> str:
    rows = "".join(_button(label, base_url + path, primary) for label, path, primary in BUTTONS)
    return f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin:14px 0">{rows}</table>'


def render(body: str) -> str:
    """HTML for a plain-text email body. Everything from the text is escaped; only the structure here is markup."""
    parts: list[str] = []
    for paragraph in body.replace("\r\n", "\n").split("\n\n"):
        lines = paragraph.split("\n")
        text_lines: list[str] = []
        for line in lines:
            match = _RESPONSE_LINE.match(line.strip())
            if match:
                if text_lines:
                    parts.append(f'<p style="margin:0 0 12px">{"<br>".join(_linkify(html.escape(t)) for t in text_lines)}</p>')
                    text_lines = []
                parts.append(_button_block(match.group(1)))
            else:
                text_lines.append(line)
        if text_lines and any(t.strip() for t in text_lines):
            parts.append(f'<p style="margin:0 0 12px">{"<br>".join(_linkify(html.escape(t)) for t in text_lines)}</p>')
    return (f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:{INK};max-width:560px">'
            + "".join(parts) + "</div>")
