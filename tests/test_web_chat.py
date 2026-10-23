import io
import itertools

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
from db.models import ChatSession, Company, IntakeSession, Job, User
from db.session import get_session
from server import main, web_chat
from server.agent import intake
from server.agent.intake import IntakeUpdate

counter = itertools.count(1)


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    for limiter in (web_chat.start_limiter, web_chat.message_limiter_ip, web_chat.message_limiter_token):
        limiter.hits.clear()
    monkeypatch.setattr(config, "DAILY_QUOTE_LIMIT", 10)
    monkeypatch.setattr(config, "GLOBAL_DAILY_JOBS", 1000)
    monkeypatch.setattr(config, "IP_DAILY_SESSIONS", 1000)
    monkeypatch.setattr(config, "MAX_MESSAGES_PER_SESSION", 60)


FULL = dict(
    category="plumber",
    title="Fix leaking bathroom tap",
    description="Bathroom vanity tap drips constantly.",
    location_text="Carlton VIC 3053",
    user_name="Irene",
    next_question=None,
)


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    monkeypatch.setattr(intake, "run_intake_model", lambda draft, transcript, text, n: IntakeUpdate(**FULL))


def new_mobile():
    return f"04{10000000 + next(counter) * 7:08d}"  # unique valid-looking AU mobiles


def start(client, mobile=None, email="irene@example.com", consent=True):
    return client.post(
        "/api/chat/start", json={"mobile": new_mobile() if mobile is None else mobile, "email": email, "consent": consent}
    )


def say(client, token, text="", files=None):
    return client.post("/api/chat/message", data={"token": token, "text": text}, files=files or [])


def confirmed_job(client, mobile):
    token = start(client, mobile).json()["token"]
    assert "Reply YES" in say(client, token, "plumber for a leaking tap in carlton").json()["replies"][0]
    r = say(client, token, "yes").json()
    assert r["state"] == "confirmed"
    return token, r


def jpeg_bytes(size=(40, 30), exif=False, fmt="JPEG"):
    buf = io.BytesIO()
    img = Image.new("RGB", size, (200, 50, 50))
    if exif:
        e = Image.Exif()
        e[0x010F] = "SecretCamMaker"
        e[0x8825] = {1: "S", 2: (37.0, 49.0, 0.0), 3: "E", 4: (144.0, 58.0, 0.0)}  # GPS block
        img.save(buf, format=fmt, exif=e)
    else:
        img.save(buf, format=fmt)
    return buf.getvalue()


# ---- start -----------------------------------------------------------------------------------------------
def test_start_requires_consent(client):
    r = start(client, consent=False)
    assert r.status_code == 400 and r.json()["error"] == "consent"


@pytest.mark.parametrize("bad", ["12345", "not a number", "", "02 9123 4567"])  # last one is a landline
def test_start_rejects_bad_mobiles(client, bad):
    r = start(client, mobile=bad)
    assert r.status_code == 400 and r.json()["error"] == "mobile"


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "a b@c.com"])
def test_start_rejects_bad_emails(client, bad):
    r = start(client, email=bad)
    assert r.status_code == 400 and r.json()["error"] == "email"


def test_start_normalises_mobile_and_hashes_the_token(client):
    r = start(client, mobile="0412 345 678")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "idle" and body["messages"][0]["role"] == "bot"
    db = get_session()
    try:
        chat = db.query(ChatSession).filter(ChatSession.token_hash != body["token"]).order_by(ChatSession.created_at.desc()).first()
        user = db.get(User, chat.user_id)
        assert user.mobile == "+61412345678" and user.email == "irene@example.com" and not user.mobile_verified
        assert db.query(ChatSession).filter_by(token_hash=body["token"]).count() == 0  # raw token is never stored
    finally:
        db.close()


def test_normalise_mobile_formats():
    assert web_chat.normalise_mobile("0412345678") == "+61412345678"
    assert web_chat.normalise_mobile("+61 412 345 678") == "+61412345678"
    with pytest.raises(ValueError):
        web_chat.normalise_mobile("0291234567")


# ---- auth ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("token", ["", "garbage", "x" * 500])
def test_message_with_bad_token_is_401(client, token):
    # An empty token is rejected by request validation (422) before reaching our auth check (401); both are refusals.
    assert say(client, token, "hello").status_code in (401, 422)
    if token:
        assert say(client, token, "hello").status_code == 401


def test_state_with_bad_token_is_401(client):
    assert client.get("/api/chat/state", params={"token": "nope"}).status_code == 401


# ---- the conversation ---------------------------------------------------------------------------------
def test_full_flow_creates_job_with_track_link(client):
    token, result = confirmed_job(client, new_mobile())
    assert result["job"]["ref"] in result["replies"][0]
    assert result["job"]["track_url"] in result["replies"][0]
    db = get_session()
    try:
        job = db.query(Job).filter_by(ref_prefix=result["job"]["ref"]).one()
        assert job.status == "QUEUED" and job.user_id and job.public_token
    finally:
        db.close()


def test_state_restores_history(client):
    token = start(client).json()["token"]
    say(client, token, "plumber for a leaking tap in carlton")
    body = client.get("/api/chat/state", params={"token": token}).json()
    roles = [m["role"] for m in body["messages"]]
    assert roles[0] == "bot" and "user" in roles
    assert body["state"] == "awaiting_confirm"


def test_chats_with_the_same_mobile_cannot_read_each_other(client):
    mobile = new_mobile()
    first = start(client, mobile).json()["token"]
    say(client, first, "my secret job: replace the vault door at 12 Private Street")
    second = start(client, mobile).json()["token"]
    seen = client.get("/api/chat/state", params={"token": second}).json()["messages"]
    assert all("vault door" not in m["text"] for m in seen)


def test_empty_and_overlong_messages_rejected(client):
    token = start(client).json()["token"]
    assert say(client, token, "  ").status_code == 400
    assert say(client, token, "x" * 2001).json()["error"] == "too_long"


def test_session_message_cap(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_MESSAGES_PER_SESSION", 2)
    token = start(client).json()["token"]
    assert say(client, token, "one").status_code == 200
    assert say(client, token, "two").status_code == 200
    r = say(client, token, "three")
    assert r.status_code == 429 and r.json()["error"] == "session_full"


# ---- limits ----------------------------------------------------------------------------------------------
def test_daily_limit_blocks_a_new_chat_for_that_mobile(client, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTE_LIMIT", 1)
    mobile = new_mobile()
    confirmed_job(client, mobile)
    r = start(client, mobile)
    assert r.status_code == 429 and r.json()["error"] == "limit_reached"
    assert "limit" in r.json()["message"].lower()
    assert start(client, new_mobile()).status_code == 200  # other numbers are unaffected


def test_daily_limit_is_also_enforced_at_confirmation(client, monkeypatch):
    """Two chats opened before either is confirmed must not both get through a limit of 1."""
    monkeypatch.setattr(config, "DAILY_QUOTE_LIMIT", 1)
    mobile = new_mobile()
    a = start(client, mobile).json()["token"]
    b = start(client, mobile).json()["token"]
    say(client, a, "plumber carlton leaking tap")
    say(client, b, "plumber carlton leaking tap")
    assert say(client, a, "yes").json()["state"] == "confirmed"
    r = say(client, b, "yes").json()
    assert r["state"] == "limit_reached"
    db = get_session()
    try:
        assert db.query(Job).join(User, Job.user_id == User.id).filter(User.mobile == web_chat.normalise_mobile(mobile)).count() == 1
    finally:
        db.close()


def test_global_cap_stops_new_chats(client, monkeypatch):
    confirmed_job(client, new_mobile())
    monkeypatch.setattr(config, "GLOBAL_DAILY_JOBS", 1)
    r = start(client)
    assert r.status_code == 503 and r.json()["error"] == "busy"


def test_ip_session_cap(client, monkeypatch):
    db = get_session()
    try:
        already = db.query(ChatSession).filter(ChatSession.ip == "testclient").count()
    finally:
        db.close()
    monkeypatch.setattr(config, "IP_DAILY_SESSIONS", already + 2)
    assert start(client).status_code == 200
    assert start(client).status_code == 200
    r = start(client)
    assert r.status_code == 429 and r.json()["error"] == "slow_down"


def test_sliding_window_limiter():
    w = web_chat.SlidingWindow(limit=2, seconds=60)
    assert w.allow("k") and w.allow("k") and not w.allow("k")
    assert w.allow("other")


# ---- photos ----------------------------------------------------------------------------------------------
def test_photo_upload_strips_metadata_and_is_attached_to_the_job(client):
    token = start(client).json()["token"]
    raw = jpeg_bytes(exif=True)
    assert Image.open(io.BytesIO(raw)).getexif().get(0x010F) == "SecretCamMaker"  # the test input really has EXIF

    r = say(client, token, "plumber leaking tap carlton", files=[("files", ("tap.jpg", raw, "image/jpeg"))])
    assert r.status_code == 200
    assert say(client, token, "yes").json()["state"] == "confirmed"

    db = get_session()
    try:
        user = db.get(User, db.query(ChatSession).order_by(ChatSession.created_at.desc()).first().user_id)
        job = db.query(Job).filter_by(user_id=user.id).one()
        assert len(job.attachment_paths) == 1
        saved = Image.open(job.attachment_paths[0])
        assert not dict(saved.getexif()), "EXIF/GPS must not survive"
        assert b"SecretCamMaker" not in open(job.attachment_paths[0], "rb").read()
    finally:
        db.close()


def test_non_image_with_image_name_is_rejected(client):
    token = start(client).json()["token"]
    r = say(client, token, "hi", files=[("files", ("evil.jpg", b"<script>alert(1)</script>", "image/jpeg"))])
    assert r.status_code == 400 and r.json()["error"] == "bad_image"


def test_too_many_files_and_oversize_rejected(client):
    token = start(client).json()["token"]
    many = [("files", (f"{i}.jpg", jpeg_bytes(), "image/jpeg")) for i in range(7)]
    assert say(client, token, "hi", files=many).json()["error"] == "too_many_files"
    big = b"\xff\xd8\xff" + b"0" * 8_100_000
    assert say(client, token, "hi", files=[("files", ("big.jpg", big, "image/jpeg"))]).json()["error"] == "bad_image"


def test_png_and_gif_accepted_and_converted(client):
    token = start(client).json()["token"]
    files = [
        ("files", ("a.png", jpeg_bytes(fmt="PNG"), "image/png")),
        ("files", ("b.gif", jpeg_bytes(fmt="GIF"), "image/gif")),
    ]
    assert say(client, token, "plumber leaking tap carlton", files=files).status_code == 200


# ---- progress page ---------------------------------------------------------------------------------------
def test_progress_page_shows_suppliers_but_never_their_emails_and_escapes_html(client):
    token, result = confirmed_job(client, new_mobile())
    pub = result["job"]["track_url"].rsplit("/", 1)[1]
    db = get_session()
    try:
        job = db.query(Job).filter_by(public_token=pub).one()
        job.title = "<script>alert('x')</script>"
        db.add(Company(job_id=job.id, place_id=f"p{next(counter)}", name="Mick's <b>Plumbing</b>", discovered_email="mick@secret-plumbing.example"))
        db.commit()
    finally:
        db.close()
    page = client.get(f"/j/{pub}")
    assert page.status_code == 200
    html = page.text
    assert "secret-plumbing.example" not in html
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "Queued" in html
    assert "no-referrer" == page.headers["Referrer-Policy"]


def test_progress_page_unknown_token_404(client):
    assert client.get("/j/does-not-exist").status_code == 404


# ---- site wide ------------------------------------------------------------------------------------------
def test_home_page_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "Quoteschmote" in r.text
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/static/chat.js").status_code == 200
    assert client.get("/docs").status_code == 404  # no public API docs
