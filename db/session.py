from __future__ import annotations

from pathlib import Path

import config  # noqa: F401  (loads .env before anything reads the environment)
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "app.db"


def _database_url() -> str:
    url = config.env("DATABASE_URL")
    if not url:
        return f"sqlite:///{DB_PATH}"
    # Hosts (Railway/Heroku) hand out "postgres://" or "postgresql://"; we use the psycopg3 driver.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DATABASE_URL = _database_url()
_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

Base.metadata.create_all(engine)


def _run_lightweight_migrations() -> None:
    """Adds columns that models gained after their table already existed. Additive only, no data loss.

    A stopgap for the MVP; swap for Alembic before the schema starts changing in non-additive ways.
    """
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}'))


_run_lightweight_migrations()


def get_session() -> Session:
    return SessionLocal()
