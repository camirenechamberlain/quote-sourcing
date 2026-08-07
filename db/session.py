from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "app.db"

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

Base.metadata.create_all(engine)


def _run_lightweight_migrations() -> None:
    """Adds columns introduced after a table already existed. SQLite-only, additive, no data loss."""
    inspector = inspect(engine)
    if not inspector.has_table("jobs"):
        return
    existing_cols = {c["name"] for c in inspector.get_columns("jobs")}
    if "attachment_paths" not in existing_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN attachment_paths JSON"))


_run_lightweight_migrations()


def get_session() -> Session:
    return SessionLocal()
