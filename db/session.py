from __future__ import annotations

import json
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


def _backfill_value(column):
    """The constant to fill into existing rows when a column is added, or None if there isn't a sensible one. Only plain constants
    (booleans, numbers, short strings, empty lists) are backfilled: never unique or key columns, and never timestamps or random tokens."""
    default = column.default
    if default is None or column.unique or column.primary_key:
        return None
    value = default.arg if getattr(default, "is_scalar", False) else None
    if value is None and getattr(default, "is_callable", False):
        try:
            value = default.arg(None)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return None


def _run_lightweight_migrations(bind=None, metadata=None) -> None:
    """Adds columns that models gained after their table already existed, giving existing rows the column's default (otherwise they
    would hold NULL and quietly fall out of every `= False` / `= 'quote'` style filter). Additive only, no data loss.

    A stopgap for the MVP; swap for Alembic before the schema starts changing in non-additive ways.
    """
    bind = bind or engine
    metadata = metadata or Base.metadata
    inspector = inspect(bind)
    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=bind.dialect)
            with bind.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {col_type}'))
                fill = _backfill_value(column)
                if fill is not None:
                    conn.execute(text(f'UPDATE {table.name} SET "{column.name}" = :v WHERE "{column.name}" IS NULL'), {"v": fill})


_run_lightweight_migrations()


def get_session() -> Session:
    return SessionLocal()
