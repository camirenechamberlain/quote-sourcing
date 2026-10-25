"""Adding a column to an existing table must give the rows that already exist a sensible value, not NULL."""
import uuid

from sqlalchemy import JSON, Boolean, Column, Float, Integer, MetaData, String, Table, create_engine, text

from db.session import _run_lightweight_migrations


def test_new_columns_are_backfilled_with_their_defaults_but_never_unique_or_random_ones():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:  # the table as it was before the new columns existed, with two rows already in it
        conn.execute(text("CREATE TABLE things (id TEXT PRIMARY KEY)"))
        conn.execute(text("INSERT INTO things (id) VALUES ('a'), ('b')"))

    meta = MetaData()
    Table("things", meta,
          Column("id", String, primary_key=True),
          Column("flag", Boolean, default=False),
          Column("kind", String, default="quote"),
          Column("count", Integer, default=0),
          Column("ratio", Float, default=1.5),
          Column("items", JSON, default=list),
          Column("token", String, unique=True, default=lambda: uuid.uuid4().hex),  # would collide if backfilled with one value
          Column("note", String, nullable=True))  # no default: stays NULL

    _run_lightweight_migrations(bind=engine, metadata=meta)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, flag, kind, count, ratio, items, token, note FROM things ORDER BY id")).fetchall()
    assert rows[0][:6] == ("a", 0, "quote", 0, 1.5, "[]") and rows[1][:6] == ("b", 0, "quote", 0, 1.5, "[]")
    assert rows[0][6] is None and rows[1][6] is None and rows[0][7] is None  # unique/random and default-less columns are left alone


def test_running_the_migration_again_changes_nothing():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE things (id TEXT PRIMARY KEY)"))
        conn.execute(text("INSERT INTO things (id) VALUES ('a')"))
    meta = MetaData()
    Table("things", meta, Column("id", String, primary_key=True), Column("flag", Boolean, default=False))
    _run_lightweight_migrations(bind=engine, metadata=meta)
    with engine.begin() as conn:
        conn.execute(text("UPDATE things SET flag = 1"))  # a real change made after the migration
    _run_lightweight_migrations(bind=engine, metadata=meta)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT flag FROM things")).scalar() == 1
