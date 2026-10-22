import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

import config
from db.models import User, WaMessage
from db.session import get_session
from server import main, whatsapp
from server.agent import intake
from server.agent.intake import IntakeUpdate

URL = "https://example.test/whatsapp/webhook"


@pytest.fixture()
def client():
    return TestClient(main.app)


def signed_post(client, form, sign=True, signature=None):
    headers = {}
    if sign:
        headers["X-Twilio-Signature"] = signature or RequestValidator(config.TWILIO_AUTH_TOKEN).compute_signature(URL, form)
    return client.post("/whatsapp/webhook", data=form, headers=headers)


def form(sid, sender="whatsapp:+61400111222", body="hello", **extra):
    return {"MessageSid": sid, "From": sender, "Body": body, "NumMedia": "0", **extra}


@pytest.fixture(autouse=True)
def fake_intake_model(monkeypatch):
    monkeypatch.setattr(
        intake,
        "run_intake_model",
        lambda draft, transcript, text, n: IntakeUpdate(is_job_request=False, next_question="What do you need done?"),
    )


def count(model, **filters):
    db = get_session()
    try:
        return db.query(model).filter_by(**filters).count()
    finally:
        db.close()


def test_missing_signature_is_rejected(client):
    r = signed_post(client, form("SM_nosig"), sign=False)
    assert r.status_code == 403
    assert count(WaMessage, twilio_sid="SM_nosig") == 0


def test_wrong_signature_is_rejected(client):
    r = signed_post(client, form("SM_bad"), signature="AAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    assert r.status_code == 403
    assert count(WaMessage, twilio_sid="SM_bad") == 0


def test_signature_for_a_tampered_body_is_rejected(client):
    good = form("SM_tamper", body="plumber please")
    sig = RequestValidator(config.TWILIO_AUTH_TOKEN).compute_signature(URL, good)
    r = signed_post(client, {**good, "Body": "send money"}, signature=sig)
    assert r.status_code == 403


def test_valid_message_is_stored_and_answered(client):
    sender = "whatsapp:+61400333444"
    r = signed_post(client, form("SM_ok1", sender=sender, body="hi"))
    assert r.status_code == 200

    db = get_session()
    try:
        user = db.query(User).filter_by(whatsapp_number=sender).one()
        inbound = db.query(WaMessage).filter_by(twilio_sid="SM_ok1").one()
        assert inbound.direction == "in" and inbound.body == "hi"
        replies = db.query(WaMessage).filter_by(user_id=user.id, direction="out").all()
        assert [m.body for m in replies] == ["What do you need done?"]
        assert replies[0].delivery == "dry_run"  # nothing actually left the building
    finally:
        db.close()


def test_twilio_retry_with_same_sid_is_processed_once(client):
    sender = "whatsapp:+61400555666"
    assert signed_post(client, form("SM_dupe", sender=sender)).status_code == 200
    assert signed_post(client, form("SM_dupe", sender=sender)).status_code == 200
    assert count(WaMessage, twilio_sid="SM_dupe") == 1

    db = get_session()
    try:
        user = db.query(User).filter_by(whatsapp_number=sender).one()
        assert db.query(WaMessage).filter_by(user_id=user.id, direction="out").count() == 1
    finally:
        db.close()


def test_non_allowlisted_number_is_silently_ignored(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_NUMBERS", ["+61400999000"])
    stranger = "whatsapp:+61400777888"
    r = signed_post(client, form("SM_stranger", sender=stranger))
    assert r.status_code == 200
    assert count(User, whatsapp_number=stranger) == 0
    assert count(WaMessage, twilio_sid="SM_stranger") == 0


def test_allowlisted_number_works(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_NUMBERS", ["+61400999000"])
    r = signed_post(client, form("SM_vip", sender="whatsapp:+61400999000"))
    assert r.status_code == 200
    assert count(WaMessage, twilio_sid="SM_vip") == 1


def test_photos_are_downloaded_and_attached_to_the_session(client, monkeypatch, tmp_path):
    saved = tmp_path / "pic.jpg"
    saved.write_bytes(b"jpeg")
    calls = []

    def fake_download(url, ctype, dest):
        calls.append((url, ctype))
        return saved if ctype.startswith("image/") else None

    monkeypatch.setattr(whatsapp, "download_media", fake_download)
    sender = "whatsapp:+61400121212"
    r = signed_post(
        client,
        form(
            "SM_media",
            sender=sender,
            body="",
            NumMedia="2",
            MediaUrl0="https://api.twilio.test/m/1",
            MediaContentType0="image/jpeg",
            MediaUrl1="https://api.twilio.test/m/2",
            MediaContentType1="application/pdf",
        ),
    )
    assert r.status_code == 200
    assert len(calls) == 2
    db = get_session()
    try:
        msg = db.query(WaMessage).filter_by(twilio_sid="SM_media").one()
        assert msg.media_paths == [str(saved)]  # the PDF was not accepted
    finally:
        db.close()


def test_media_download_rejects_non_images(tmp_path):
    assert whatsapp.download_media("https://x.test/f", "application/pdf", tmp_path) is None
    assert whatsapp.download_media("https://x.test/f", "text/html", tmp_path) is None


def test_healthz_reports_dry_run(client):
    assert client.get("/healthz").json() == {"ok": True, "outbound_mode": "dry_run"}
