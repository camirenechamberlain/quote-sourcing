"""Cache-busting for static files.

Cloudflare (and browsers) cache /static files for hours. Giving every asset URL a `?v=<last-modified>` token means a
changed file always has a new URL, so visitors never see a stale stylesheet or script after a deploy.
"""
from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"


def version() -> str:
    return str(int(max((p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file()), default=0)))


def asset(name: str) -> str:
    return f"/static/{name}?v={version()}"


def versioned_html(filename: str) -> str:
    html = (STATIC_DIR / filename).read_text(encoding="utf-8")
    for name in ("chat.css", "chat.js", "favicon.svg"):
        html = html.replace(f'"/static/{name}"', f'"{asset(name)}"')
    return html
