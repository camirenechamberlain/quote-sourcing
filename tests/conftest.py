"""Test environment. Must set env vars BEFORE the app modules are imported (config/db read them at import)."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="quote_sourcing_tests_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp / 'test.db'}"
os.environ["TWILIO_AUTH_TOKEN"] = "test-auth-token"
os.environ["TWILIO_ACCOUNT_SID"] = ""
os.environ["PUBLIC_BASE_URL"] = "https://example.test"
os.environ["OUTBOUND_MODE"] = "dry_run"
os.environ["ALLOWED_NUMBERS"] = ""
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

    u = User(whatsapp_number=f"whatsapp:+61400{uuid.uuid4().int % 1000000:06d}")
    db.add(u)
    db.commit()
    return u
