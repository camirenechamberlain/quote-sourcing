"""Customer accounts: email sign-in links, sessions, and the "My requests" dashboard."""
import datetime
import secrets

import pytest
from fastapi.testclient import TestClient

from db.models import AccountLoginToken, AccountSession, Job, Notification, User
from db.session import get_session
from server import account, main
from server.web_chat import hash_token
from services import outbound


@pytest.fixture()
def client():
    return TestClient(main.app, base_url="https://testserver", follow_redirects=False)  # the cookie is Secure, so talk https


@pytest.fixture(autouse=True)
def fresh_limits():
    for limiter in (account.login_ip_limiter, account.login_email_limiter, account.verify_limiter):
        limiter.hits.clear()


@pytest.fixture()
def mail(monkeypatch):
    """Capture emails (live mode, transport replaced): (to, subject, body)."""
    sent = []
    monkeypatch.setattr(outbound, "mode", lambda: "live")
    monkeypatch.setattr(outbound, "deliver", lambda to, subject, body, attachment_paths=None, extra_files=None:
                        sent.append((to, subject, body)) or outbound.Delivery(True, "m", "t", to, False))
    return sent


def make_job(email, title="Fix tile", status="QUEUED"):
    db = get_session()
    try:
        user = User(whatsapp_number=f"web:ac{secrets.token_hex(4)}", name="Irene", email=email, mobile=f"+6140{secrets.randbelow(10**7):07d}")
        db.add(user)
        db.flush()
        job = Job(title=title, description="d", category="tiler", location_text="Reservoir VIC", user_id=user.id, status=status)
        db.add(job)
        db.commit()
        return job.public_token
    finally:
        db.close()


def link_token(mail, email):
    body = next(m[2] for m in mail if m[0] == email and "sign-in link" in m[1])
    return body.split("token=")[1].split()[0]


def sign_in(client, mail, email):
    client.post("/account/login", data={"email": email})
    token = link_token(mail, email)
    assert client.post("/account/verify", data={"token": token}).status_code == 303
    return token


def unique_email():
    return f"cust-{secrets.token_hex(4)}@example.com"


def test_the_signed_out_page_asks_for_an_email(client):
    r = client.get("/account")
    assert r.status_code == 200 and "Email me a sign-in link" in r.text and "type='email'" in r.text


def test_a_known_email_is_sent_a_link_and_the_page_never_says_whether_it_exists(client, mail):
    email = unique_email()
    make_job(email)
    known = client.post("/account/login", data={"email": email})
    unknown = client.post("/account/login", data={"email": unique_email()})
    assert known.text == unknown.text and "If that email has any requests" in known.text  # identical answers: nothing to enumerate
    assert [m[0] for m in mail] == [email]  # and only the address that really has requests is emailed
    assert "/account/verify?token=" in mail[0][2] and "15 minutes" in mail[0][2]


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "a b@example.com", "x" * 300 + "@example.com"])
def test_bad_emails_are_rejected_and_nothing_is_sent(client, mail, bad):
    r = client.post("/account/login", data={"email": bad})
    assert r.status_code == 200 and "valid email" in r.text and mail == []


def test_the_email_is_matched_whatever_its_case_and_spaces(client, mail):
    email = unique_email()
    make_job(email)
    client.post("/account/login", data={"email": f"  {email.upper()}  "})
    assert [m[0] for m in mail] == [email]


def test_opening_the_link_does_not_sign_you_in_only_the_button_does(client, mail):
    """Email scanners pre-open links; a GET must not consume the link or create a session."""
    email = unique_email()
    make_job(email)
    client.post("/account/login", data={"email": email})
    token = link_token(mail, email)
    page = client.get(f"/account/verify?token={token}")
    assert page.status_code == 200 and "Sign in" in page.text and "set-cookie" not in page.headers
    assert "Email me a sign-in link" in client.get("/account").text  # still signed out
    r = client.post("/account/verify", data={"token": token})
    assert r.status_code == 303 and account.COOKIE in r.headers["set-cookie"]
    flags = r.headers["set-cookie"].lower()
    assert "httponly" in flags and "samesite=lax" in flags and "secure" in flags


def test_a_link_works_once(client, mail):
    email = unique_email()
    make_job(email)
    token = sign_in(client, mail, email)
    again = client.post("/account/verify", data={"token": token})
    assert again.status_code == 400 and "already used" in again.text


def test_an_expired_or_made_up_link_is_refused(client, mail):
    email = unique_email()
    make_job(email)
    client.post("/account/login", data={"email": email})
    token = link_token(mail, email)
    db = get_session()
    db.query(AccountLoginToken).filter_by(token_hash=hash_token(token)).one().expires_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=1)
    db.commit()
    db.close()
    assert client.post("/account/verify", data={"token": token}).status_code == 400
    assert client.get("/account/verify?token=made-up").status_code == 400
    assert client.post("/account/verify", data={"token": "x" * 500}).status_code == 400


def test_tokens_are_stored_only_as_hashes(client, mail):
    email = unique_email()
    make_job(email)
    token = sign_in(client, mail, email)
    db = get_session()
    stored = [r.token_hash for r in db.query(AccountLoginToken).filter_by(email=email).all()]
    assert stored and token not in stored and all(len(h) == 64 for h in stored)
    cookie = client.cookies.get(account.COOKIE)
    assert db.query(AccountSession).filter_by(token_hash=hash_token(cookie)).count() == 1
    assert db.query(AccountSession).filter_by(token_hash=cookie).count() == 0


def test_the_dashboard_lists_only_that_customers_requests_newest_first(client, mail):
    email, other = unique_email(), unique_email()
    make_job(email, "Older job")
    t2 = make_job(email, "Newer job")
    make_job(other, "Someone else job")
    sign_in(client, mail, email)
    page = client.get("/account").text
    assert "Signed in as " + email in page and "Newer job" in page and "Older job" in page
    assert "Someone else" not in page
    assert page.index("Newer job") < page.index("Older job")
    assert f"/j/{t2}" in page and "Finding suppliers" in page and "Start a new request" in page


def test_one_customers_session_cannot_see_anothers_jobs_and_a_forged_cookie_gets_nothing(client, mail):
    a, b = unique_email(), unique_email()
    make_job(a, "A private job")
    make_job(b, "B private job")
    sign_in(client, mail, a)
    page = client.get("/account").text
    assert "A private job" in page and "B private job" not in page
    client.cookies.set(account.COOKIE, "forged-" + secrets.token_urlsafe(24))
    assert "Email me a sign-in link" in client.get("/account").text


def test_sessions_expire_and_signing_out_ends_them(client, mail):
    email = unique_email()
    make_job(email)
    sign_in(client, mail, email)
    cookie = client.cookies.get(account.COOKIE)
    assert client.post("/account/logout").status_code == 303
    client.cookies.set(account.COOKIE, cookie)  # replay the old cookie after signing out
    assert "Email me a sign-in link" in client.get("/account").text

    mail.clear()
    client.cookies.clear()
    account.login_email_limiter.hits.clear()
    sign_in(client, mail, email)
    cookie = client.cookies.get(account.COOKIE)
    db = get_session()
    db.query(AccountSession).filter_by(token_hash=hash_token(cookie)).one().expires_at = datetime.datetime.utcnow() - datetime.timedelta(days=1)
    db.commit()
    db.close()
    assert "Email me a sign-in link" in client.get("/account").text


def test_login_is_rate_limited_per_address_without_giving_it_away(client, mail):
    email = unique_email()
    make_job(email)
    answers = [client.post("/account/login", data={"email": email}).text for _ in range(6)]
    assert len(set(answers)) == 1  # always the same answer
    assert len([m for m in mail if m[0] == email]) == 3  # but only three emails ever go out per hour


def test_login_is_rate_limited_per_network(client, mail):
    codes = [client.post("/account/login", data={"email": unique_email()}).status_code for _ in range(account.login_ip_limiter.limit + 3)]
    assert codes[0] == 200 and codes[-1] == 429


def test_the_login_link_email_is_recorded_for_the_audit_trail(client, mail):
    email = unique_email()
    make_job(email)
    client.post("/account/login", data={"email": email})
    note = get_session().query(Notification).filter_by(to_email=email, kind="login_link").one()
    assert note.audience == "customer" and note.status == "sent"


def test_the_site_header_links_to_my_requests(client):
    assert 'href="/account"' in client.get("/").text
