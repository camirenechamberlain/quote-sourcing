from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import config
from db.session import get_session
from server import assets, customer_visits, supplier_response, web_chat
from services import followups, outbound

log = logging.getLogger("server")
logging.basicConfig(level=logging.INFO)


def run_followups_once() -> None:
    """One pass of the automated follow-ups. Never raises: a problem is logged and the next pass tries again."""
    db = get_session()
    try:
        report = followups.run(db, send=True)
        if report.planned or report.sent:
            log.info("follow-ups (%s): due=%d sent=%d %s", outbound.mode(), len(report.planned), report.sent, report.note)
    except Exception:  # noqa: BLE001
        log.exception("Follow-up run failed")
    finally:
        db.close()


async def _followup_loop() -> None:
    while True:
        await asyncio.to_thread(run_followups_once)
        await asyncio.sleep(config.FOLLOWUP_INTERVAL_MINUTES * 60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = None
    if config.FOLLOWUPS_ENABLED:
        log.info("Automated follow-ups ON, every %d minutes (mode: %s)", config.FOLLOWUP_INTERVAL_MINUTES, outbound.mode())
        task = asyncio.create_task(_followup_loop())
    yield
    if task:
        task.cancel()


app = FastAPI(title="Quoteschmote", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(web_chat.router)
app.include_router(supplier_response.router)
app.include_router(customer_visits.router)

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
