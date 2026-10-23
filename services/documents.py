"""Customer PDF handling (e.g. engineering drawings).

Like photos, PDFs end up attached to emails sent to third parties, and they arrive from the open internet. We never keep or
forward the original file. Instead the document is rebuilt page by page into a fresh PDF, which drops everything that isn't
page content: document-level actions and JavaScript, embedded files, form scripts and document metadata. Page content stays
vector, so drawings keep their detail (re-rasterising them would not).
"""
from __future__ import annotations

import io
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter

MAX_PDF_BYTES = 10_000_000
MAX_PAGES = 40
PDF_HEADER_WINDOW = 1024  # the %PDF- marker may be preceded by a few junk bytes


class BadPdf(ValueError):
    pass


def is_pdf(data: bytes) -> bool:
    """Judge by content, never by the file name or the browser's claimed type."""
    return b"%PDF-" in (data or b"")[:PDF_HEADER_WINDOW]


def _strip_actions(obj) -> None:
    """Remove anything that could run or navigate: page-level and annotation-level actions."""
    for key in ("/A", "/AA"):
        if key in obj:
            del obj[key]


def sanitise_pdf(data: bytes) -> bytes:
    """Rebuild the PDF from its pages with actions stripped. Raises BadPdf if it isn't a usable, reasonably sized PDF."""
    if not data or len(data) > MAX_PDF_BYTES:
        raise BadPdf("That PDF is empty or larger than 10MB")
    if not is_pdf(data):
        raise BadPdf("That file isn't a PDF")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise BadPdf("That PDF is password-protected. Please remove the password and try again")
        page_count = len(reader.pages)
        if page_count == 0:
            raise BadPdf("That PDF has no pages")
        if page_count > MAX_PAGES:
            raise BadPdf(f"That PDF has {page_count} pages; please keep it to {MAX_PAGES} or fewer")

        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)  # a fresh document: no catalog actions, JavaScript, embedded files or form scripts
        for page in writer.pages:
            _strip_actions(page)
            annots = page.get("/Annots")
            if annots is not None:
                for ref in annots.get_object():
                    _strip_actions(ref.get_object())
        writer.add_metadata({"/Producer": "Quoteschmote"})  # no author, title, creator or software details carried over

        out = io.BytesIO()
        writer.write(out)
        return out.getvalue()
    except BadPdf:
        raise
    except Exception as exc:  # noqa: BLE001 - any parser failure means we can't safely use the file
        raise BadPdf("That PDF couldn't be read. It may be damaged") from exc


def safe_filename(original: str | None) -> str:
    """A tidy, harmless file name for the supplier's email (the customer's own name for it, minus anything odd)."""
    stem = Path(original or "").stem
    stem = re.sub(r"[^A-Za-z0-9._ -]", "", stem).strip(" .")[:60]
    return f"{stem or 'Drawing'}.pdf"


def save_sanitised_pdf(data: bytes, dest_dir: Path, token: str, original_name: str | None) -> Path:
    clean = sanitise_pdf(data)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{token}_{safe_filename(original_name)}"
    path.write_bytes(clean)
    return path
