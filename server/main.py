from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config
from server import assets, supplier_response, web_chat

log = logging.getLogger("server")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Quoteschmote", docs_url=None, redoc_url=None, openapi_url=None)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(web_chat.router)
app.include_router(supplier_response.router)

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
