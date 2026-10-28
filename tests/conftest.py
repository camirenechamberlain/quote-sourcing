"""Test environment. Must set env vars BEFORE the app modules are imported (config/db read them at import)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="quote_sourcing_tests_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["OUTBOUND_MODE"] = "dry_run"
os.environ["ANTHROPIC_API_KEY"] = "test-not-used"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from services import attachments as attachments_service  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_attachments(monkeypatch):
    monkeypatch.setattr(attachments_service, "ATTACHMENTS_DIR", _tmp / "attachments")
    yield


@pytest.fixture()
def db():
    from db.session import get_session

    session = get_session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture()
def user(db):
    import uuid

    from db.models import User

    u = User(whatsapp_number=f"web:test{uuid.uuid4().int % 1000000:06d}")
    db.add(u)
    db.commit()
    return u


@pytest.fixture(autouse=True)
def photos_first_off_by_default(monkeypatch):
    """Most tests exercise the questions themselves, so they skip the ask-for-photos step. test_photos_first.py turns it on."""
    import config

    monkeypatch.setattr(config, "PHOTOS_FIRST", False)
    yield


@pytest.fixture(autouse=True)
def stub_supplier_review(monkeypatch):
    """The real reviewer calls the Anthropic API. Tests default to 'nothing missing'; individual tests override this."""
    from server.agent import supplier_review

    monkeypatch.setattr(supplier_review, "review_request", lambda draft, n: supplier_review.Review([], True))
    yield
