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
    monkeypatch.setattr(config, "GLOBAL_DAILY_MESSAGES", 100000)
    monkeypatch.setattr(config, "IP_DAILY_SESSIONS", 1000)
    monkeypatch.setattr(config, "MAX_MESSAGES_PER_SESSION", 60)


FULL = dict(
    category="plumber",
    title="Fix leaking bathroom tap",
    description="Bathroom vanity tap drips constantly.",
    location_text="Carlton VIC 3053",
    distance_preference="local",
    max_distance_km=10,
    rating_preference="no_preference",
    timeframe="ASAP",
    next_question=None,
)


@pytest.fixture(autouse=True)
def fake_model(monkeypatch):
    monkeypatch.setattr(intake, "run_intake_model", lambda draft, transcript, text, n: IntakeUpdate(**FULL))


def new_mobile():
    return f"04{10000000 + next(counter) * 7:08d}"  # unique valid-looking AU mobiles


def start(client):
    return client.post("/api/chat/start")


def say(client, token, text="", files=None):
    return client.post("/api/chat/message", data={"token": token, "text": text}, files=files or [])


def contact(client, token, name="Irene", mobile=None, email="irene@example.com", consent=True):
    return client.post(
        "/api/chat/contact",
        json={"token": token, "name": name, "mobile": new_mobile() if mobile is None else mobile, "email": email, "consent": consent},
    )


def refined_chat(client, files=None):
    """A chat that has been described and refined to the confirmation summary, with no contact details yet."""
    token = start(client).json()["token"]
    r = say(client, token, "plumber for a leaking tap in carlton", files=files).json()
    assert "Reply YES" in r["replies"][0], r
    assert r["state"] == "awaiting_confirm" and r["needs_details"] is True
    return token


def confirmed_job(client, mobile=None):
    token = refined_chat(client)
    assert say(client, token, "yes").json()["state"] == "need_details"
    r = contact(client, token, mobile=mobile).json()
    assert r["state"] == "confirmed", r
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


# ---- landing page + start: nothing is asked up front ---------------------------------------------------------
def test_landing_page_starts_with_the_description_not_contact_details(client):
    html = client.get("/").text
    assert 'id="text"' in html and "Describe the job" in html
    assert 'id="mobile"' not in html and 'id="email"' not in html  # no contact form on the landing page


def test_start_is_anonymous_and_the_token_is_stored_hashed(client):
    r = start(client)
    assert r.status_code == 200
    body = r.json()
    assert body["messages"] == [] and body["state"] == "idle" and body["needs_details"] is True
    db = get_session()
    try:
        assert db.query(ChatSession).filter_by(token_hash=body["token"]).count() == 0  # raw token is never stored
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(body["token"])).one()
        user = db.get(User, chat.user_id)
        assert user.mobile is None and user.email is None and user.name is None
    finally:
        db.close()


def test_start_is_rate_limited_per_ip(client):
    codes = [start(client).status_code for _ in range(12)]
    assert codes[:10] == [200] * 10 and 429 in codes[10:]


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


def test_global_message_cap_stops_new_chats_and_ongoing_messages(client, monkeypatch):
    token = start(client).json()["token"]
    monkeypatch.setattr(config, "GLOBAL_DAILY_MESSAGES", 0)
    r = start(client)
    assert r.status_code == 503 and r.json()["error"] == "busy"
    r = say(client, token, "hello")
    assert r.status_code == 503 and r.json()["error"] == "busy"


# ---- the conversation: refine first, then ask who they are -----------------------------------------------------
def test_agent_summarises_without_asking_for_a_name(client):
    token = start(client).json()["token"]
    replies = say(client, token, "plumber for a leaking tap in carlton").json()["replies"]
    assert len(replies) == 1 and "Reply YES" in replies[0]
    assert "first name" not in replies[0].lower()


def test_yes_before_details_asks_for_them_and_creates_no_job(client):
    token = refined_chat(client)
    r = say(client, token, "yes").json()
    assert r["state"] == "need_details" and r["needs_details"] is True
    assert "contact details" in r["replies"][0]
    db = get_session()
    try:
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(token)).one()
        assert db.query(Job).filter_by(user_id=chat.user_id).count() == 0
        assert db.query(IntakeSession).filter_by(user_id=chat.user_id).one().state == "awaiting_confirm"  # still open
    finally:
        db.close()


def test_full_flow_creates_job_with_the_customers_details(client):
    token, result = confirmed_job(client, mobile="0412 345 678")
    assert result["job"]["ref"] in result["replies"][0]
    assert result["job"]["track_url"] in result["replies"][0]
    assert result["needs_details"] is False
    db = get_session()
    try:
        job = db.query(Job).filter_by(ref_prefix=result["job"]["ref"]).one()
        user = db.get(User, job.user_id)
        assert job.status == "QUEUED" and job.public_token
        assert (user.name, user.mobile, user.email) == ("Irene", "+61412345678", "irene@example.com")
        assert user.mobile_verified is False
    finally:
        db.close()


def test_state_restores_history_with_no_greeting(client):
    token = start(client).json()["token"]
    say(client, token, "plumber for a leaking tap in carlton")
    body = client.get("/api/chat/state", params={"token": token}).json()
    assert [m["role"] for m in body["messages"]][0] == "user"
    assert body["state"] == "awaiting_confirm" and body["needs_details"] is True


def test_chats_cannot_read_each_other(client):
    first = start(client).json()["token"]
    say(client, first, "my secret job: replace the vault door at 12 Private Street")
    second = start(client).json()["token"]
    seen = client.get("/api/chat/state", params={"token": second}).json()["messages"]
    assert seen == []


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


@pytest.mark.parametrize("token", ["", "garbage", "x" * 500])
def test_message_with_bad_token_is_refused(client, token):
    assert say(client, token, "hello").status_code in (401, 422)  # empty token fails request validation first
    if token:
        assert say(client, token, "hello").status_code == 401


def test_state_with_bad_token_is_401(client):
    assert client.get("/api/chat/state", params={"token": "nope"}).status_code == 401


# ---- contact step validation ------------------------------------------------------------------------------------
def test_contact_requires_a_refined_request(client):
    token = start(client).json()["token"]
    r = contact(client, token)
    assert r.status_code == 400 and r.json()["error"] == "not_ready"


def test_contact_with_bad_token_is_401(client):
    assert contact(client, "garbage").status_code == 401


def test_contact_requires_consent(client):
    token = refined_chat(client)
    r = contact(client, token, consent=False)
    assert r.status_code == 400 and r.json()["error"] == "consent"


@pytest.mark.parametrize("bad", ["", "   ", "123", "!!!"])
def test_contact_rejects_bad_names(client, bad):
    token = refined_chat(client)
    r = contact(client, token, name=bad)
    assert r.status_code == 400 and r.json()["error"] == "name"


@pytest.mark.parametrize("bad", ["12345", "not a number", "", "02 9123 4567"])  # last one is a landline
def test_contact_rejects_bad_mobiles(client, bad):
    token = refined_chat(client)
    r = contact(client, token, mobile=bad)
    assert r.status_code == 400 and r.json()["error"] == "mobile"


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "a b@c.com"])
def test_contact_rejects_bad_emails(client, bad):
    token = refined_chat(client)
    r = contact(client, token, email=bad)
    assert r.status_code == 400 and r.json()["error"] == "email"


def test_rejected_contact_attempt_leaves_the_request_open_for_a_retry(client):
    token = refined_chat(client)
    assert contact(client, token, mobile="12345").status_code == 400
    assert contact(client, token).json()["state"] == "confirmed"


def test_contact_name_is_cleaned_to_a_first_name(client):
    token = refined_chat(client)
    contact(client, token, name="  mary-jane   o'brien ")
    db = get_session()
    try:
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(token)).one()
        assert db.get(User, chat.user_id).name == "Mary-Jane"
    finally:
        db.close()


def test_normalise_mobile_formats():
    assert web_chat.normalise_mobile("0412345678") == "+61412345678"
    assert web_chat.normalise_mobile("+61 412 345 678") == "+61412345678"
    with pytest.raises(ValueError):
        web_chat.normalise_mobile("0291234567")


# ---- limits are enforced when the request would become a job --------------------------------------------------
def test_daily_limit_is_enforced_at_the_contact_step(client, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTE_LIMIT", 1)
    mobile = new_mobile()
    confirmed_job(client, mobile)

    token = refined_chat(client)  # a second chat can be described freely...
    r = contact(client, token, mobile=mobile)  # ...but cannot become a second job
    assert r.status_code == 429 and r.json()["error"] == "limit_reached" and r.json()["state"] == "limit_reached"
    assert "limit" in r.json()["message"].lower()

    db = get_session()
    try:
        n = db.query(Job).join(User, Job.user_id == User.id).filter(User.mobile == web_chat.normalise_mobile(mobile)).count()
        assert n == 1
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(token)).one()
        assert db.query(IntakeSession).filter_by(user_id=chat.user_id).one().state == "cancelled"
    finally:
        db.close()

    # other numbers are unaffected
    assert contact(client, refined_chat(client)).json()["state"] == "confirmed"


def test_two_chats_open_at_once_cannot_both_get_past_a_limit_of_one(client, monkeypatch):
    monkeypatch.setattr(config, "DAILY_QUOTE_LIMIT", 1)
    mobile = new_mobile()
    a, b = refined_chat(client), refined_chat(client)
    assert contact(client, a, mobile=mobile).json()["state"] == "confirmed"
    assert contact(client, b, mobile=mobile).json()["state"] == "limit_reached"


def test_global_job_cap_is_enforced_at_the_contact_step(client, monkeypatch):
    token = refined_chat(client)
    confirmed_job(client)
    monkeypatch.setattr(config, "GLOBAL_DAILY_JOBS", 1)
    r = contact(client, token)
    assert r.status_code == 503 and r.json()["error"] == "busy"


def test_sliding_window_limiter():
    w = web_chat.SlidingWindow(limit=2, seconds=60)
    assert w.allow("k") and w.allow("k") and not w.allow("k")
    assert w.allow("other")


# ---- photos ----------------------------------------------------------------------------------------------------
def test_photo_upload_strips_metadata_and_is_attached_to_the_job(client):
    raw = jpeg_bytes(exif=True)
    assert Image.open(io.BytesIO(raw)).getexif().get(0x010F) == "SecretCamMaker"  # the test input really has EXIF

    token = refined_chat(client, files=[("files", ("tap.jpg", raw, "image/jpeg"))])
    assert say(client, token, "yes").json()["state"] == "need_details"
    assert contact(client, token).json()["state"] == "confirmed"

    db = get_session()
    try:
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(token)).one()
        job = db.query(Job).filter_by(user_id=chat.user_id).one()
        assert len(job.attachment_paths) == 1
        assert not dict(Image.open(job.attachment_paths[0]).getexif()), "EXIF/GPS must not survive"
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


# ---- progress page ---------------------------------------------------------------------------------------------
def test_progress_page_shows_suppliers_but_never_their_emails_and_escapes_html(client):
    token, result = confirmed_job(client)
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
    assert page.headers["Referrer-Policy"] == "no-referrer"


def test_progress_page_unknown_token_404(client):
    assert client.get("/j/does-not-exist").status_code == 404


# ---- site wide ------------------------------------------------------------------------------------------------
def test_home_page_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "Quoteschmote" in r.text
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/static/chat.js").status_code == 200
    assert client.get("/docs").status_code == 404  # no public API docs


# ---- supplier review on the website: it runs before the contact card -------------------------------------------
def gap_reviewer(monkeypatch):
    from server.agent import supplier_review

    calls = []

    def fake(draft, n_photos):
        calls.append(draft)
        return supplier_review.Review(["How many storeys is the house?"], True)

    monkeypatch.setattr(supplier_review, "review_request", fake)
    return calls


def test_review_questions_come_before_the_contact_card(client, monkeypatch):
    calls = gap_reviewer(monkeypatch)
    token = refined_chat(client)

    r = say(client, token, "yes").json()
    assert "How many storeys is the house?" in r["replies"][0]
    assert r["state"] == "collecting" and r["review_pending"] is True  # not need_details yet
    assert len(calls) == 1

    # the customer answers; the summary comes back; yes now moves on to contact details, with no second review
    assert "Reply YES" in say(client, token, "single storey").json()["replies"][0]
    assert say(client, token, "yes").json()["state"] == "need_details"
    assert len(calls) == 1
    assert contact(client, token).json()["state"] == "confirmed"


def test_send_as_is_skips_the_review_questions_on_the_website(client, monkeypatch):
    gap_reviewer(monkeypatch)
    token = refined_chat(client)
    assert say(client, token, "yes").json()["review_pending"] is True

    r = say(client, token, "Send as is").json()
    assert r["state"] == "need_details" and r["review_pending"] is False
    final = contact(client, token).json()
    assert final["state"] == "confirmed"
    db = get_session()
    try:
        assert db.query(Job).filter_by(ref_prefix=final["job"]["ref"]).one().status == "QUEUED"
    finally:
        db.close()
