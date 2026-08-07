from __future__ import annotations

import uuid
from pathlib import Path

ATTACHMENTS_DIR = Path(__file__).resolve().parent.parent / "data" / "attachments"
MAX_TOTAL_BYTES = 15_000_000  # soft cap, leaves headroom under Gmail's ~25MB raw-message limit
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}


def job_attachment_dir(job_id: str) -> Path:
    d = ATTACHMENTS_DIR / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_uploaded_files(job_id: str, uploaded_files: list) -> list[str]:
    """uploaded_files: Streamlit UploadedFile objects. Returns saved absolute paths."""
    d = job_attachment_dir(job_id)
    saved = []
    for f in uploaded_files:
        safe_name = Path(f.name).name  # strip any path components from the browser-supplied filename
        if Path(safe_name).suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        dest = d / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        dest.write_bytes(f.getvalue())
        saved.append(str(dest))
    return saved


def delete_attachment(path_str: str) -> None:
    path = Path(path_str)
    if path.exists():
        path.unlink()


def total_size(paths: list[str]) -> int:
    return sum(Path(p).stat().st_size for p in paths if Path(p).exists())
