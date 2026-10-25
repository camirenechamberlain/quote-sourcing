"""The supplier's no-login response page (/q/<token>) and how it shows up for the customer."""
import datetime
import secrets

import pytest
from fastapi.testclient import TestClient

from db.models import Company, Job, Quote, ReplyEmail, RfqSend, User
from db.session import get_session
from server import main, supplier_response
from services import timeutil
from services.rfq_template import build_body, build_rfq
from types import SimpleNamespace


@pytest.fixture()
def client():
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def fresh_limits():
    for limiter in (supplier_response.view_limiter, supplier_response.submit_limiter_ip, supplier_response.submit_limiter_token):
        limiter.hits.clear()


@pytest.fixture()
def rfq():
    """A job, a supplier and a SENT request with a response token. Returns (token, job_public_token, ids)."""
    db = get_session()
    try:
        user = User(whatsapp_number=f"web:test{secrets.token_hex(4)}", name="Irene")
        db.add(user)
        db.flush()
        job = Job(title="Replace driveway", description="Replace the cracked <b>concrete</b> driveway.\nAbout 30 sqm.",
                  category="concreter", location_text="Fitzroy VIC 3065", project_type="residential", timeframe="Within a month",
                  user_id=user.id, status="QUEUED")
        db.add(job)
        db.flush()
        company = Company(job_id=job.id, place_id=f"p-{secrets.token_hex(4)}", name="Acme Concreting", discovered_email="acme@example.com")
        db.add(company)
        db.flush()
        token = secrets.token_urlsafe(16)
        send = RfqSend(job_id=job.id, company_id=company.id, reference_code=f"{job.ref_prefix}-ABC123", recipient_email="acme@example.com",
                       subject="Request for Quote", body="x", status="sent", response_token=token)
        db.add(send)
        db.commit()
        return token, job.public_token, {"job": job.id, "company": company.id, "rfq": send.id}
    finally:
        db.close()


def stored(rfq_id):
    db = get_session()
    try:
        reply = db.query(ReplyEmail).filter_by(rfq_send_id=rfq_id).all()
        quotes = db.query(Quote).join(ReplyEmail, Quote.reply_email_id == ReplyEmail.id).filter(ReplyEmail.rfq_send_id == rfq_id).all()
        return reply, quotes
    finally:
        db.close()


QUOTE = {"price_type": "fixed", "price": "$4,800", "price_max": "", "included": "Demolition\nNew 100mm slab", "excluded": "Fencing",
         "timeline": "3 days", "warranty": "7 years", "notes": "Can start in 2 weeks"}


# ---- viewing -------------------------------------------------------------------------------------------------
def test_landing_shows_the_brief_escaped_and_the_three_choices(client, rfq):
    token, _, _ = rfq
    r = client.get(f"/q/{token}")
    assert r.status_code == 200
    assert "Replace driveway" in r.text and "Fitzroy VIC 3065" in r.text and "on behalf of Irene" in r.text
    assert "&lt;b&gt;concrete&lt;/b&gt;" in r.text and "<b>concrete</b>" not in r.text  # customer text can never inject markup
    for path in ("/quote", "/visit", "/decline"):
        assert f"/q/{token}{path}" in r.text


def test_pages_are_never_indexed_or_cached(client, rfq):
    token, _, _ = rfq
    r = client.get(f"/q/{token}")
    assert "noindex" in r.headers["X-Robots-Tag"] and r.headers["Cache-Control"] == "no-store"
    assert 'name="robots" content="noindex' in r.text


@pytest.mark.parametrize("token", ["nope", "x" * 20, "../etc/passwd", "a" * 200, "short"])
def test_unknown_or_malformed_tokens_are_a_plain_404(client, token):
    assert client.get(f"/q/{token}").status_code == 404
    assert client.post(f"/q/{token}/quote", data=QUOTE).status_code == 404


def test_a_request_that_was_never_sent_has_no_live_link(client, rfq):
    token, _, ids = rfq
    db = get_session()
    db.get(RfqSend, ids["rfq"]).status = "failed"
    db.commit()
    db.close()
    assert client.get(f"/q/{token}").status_code == 404


# ---- quoting -------------------------------------------------------------------------------------------------
def test_a_quote_is_stored_like_an_emailed_reply_and_confirmed(client, rfq):
    token, _, ids = rfq
    r = client.post(f"/q/{token}/quote", data=QUOTE)
    assert r.status_code == 303 and r.headers["location"] == f"/q/{token}?saved=1"
    assert "Thanks, your response is with the customer" in client.get(r.headers["location"]).text

    replies, quotes = stored(ids["rfq"])
    assert len(replies) == 1 and replies[0].source == "web" and replies[0].gmail_message_id == f"web-{ids['rfq']}"
    q = quotes[0]
    assert q.kind == "quote" and q.is_quote and q.price_amount == 4800 and q.price_type == "fixed" and q.price_max is None
    assert q.scope_included == ["Demolition", "New 100mm slab"] and q.exclusions == ["Fencing"]
    assert q.timeline_text == "3 days" and q.warranty_notes == "7 years" and q.confidence == "high" and q.currency == "AUD"
    assert q.company_id == ids["company"] and q.job_id == ids["job"]


def test_a_range_and_an_hourly_rate(client, rfq):
    token, _, ids = rfq
    client.post(f"/q/{token}/quote", data={**QUOTE, "price_type": "range", "price": "4800", "price_max": "5,600"})
    q = stored(ids["rfq"])[1][0]
    assert (q.price_type, q.price_amount, q.price_max) == ("range", 4800, 5600)
    client.post(f"/q/{token}/quote", data={**QUOTE, "price_type": "hourly", "price": "95", "price_max": "999"})
    q = stored(ids["rfq"])[1][0]
    assert (q.price_type, q.price_amount, q.price_max) == ("hourly", 95, None)  # an upper price means nothing for an hourly rate


def test_submitting_again_replaces_the_response_instead_of_duplicating_it(client, rfq):
    token, _, ids = rfq
    client.post(f"/q/{token}/quote", data=QUOTE)
    client.post(f"/q/{token}/quote", data={**QUOTE, "price": "4500"})
    client.post(f"/q/{token}/decline", data={"reason": "too_busy"})  # changing their mind switches the kind too
    replies, quotes = stored(ids["rfq"])
    assert len(replies) == 1 and len(quotes) == 1
    assert quotes[0].kind == "decline" and not quotes[0].is_quote and quotes[0].price_amount is None


@pytest.mark.parametrize("price", ["", "abc", "0", "-5", "12e3", "99999999999", "4,8,0"])
def test_bad_prices_are_rejected_with_a_message_and_nothing_is_stored(client, rfq, price):
    token, _, ids = rfq
    r = client.post(f"/q/{token}/quote", data={**QUOTE, "price": price})
    assert r.status_code == 400 and "enter your price as a number" in r.text
    assert price == "" or f'value="{price}"' in r.text  # what they typed is kept so they can fix it
    assert stored(ids["rfq"]) == ([], [])


def test_a_range_needs_a_higher_upper_price(client, rfq):
    token, _, ids = rfq
    r = client.post(f"/q/{token}/quote", data={**QUOTE, "price_type": "range", "price": "5000", "price_max": "4000"})
    assert r.status_code == 400 and "upper price" in r.text and stored(ids["rfq"]) == ([], [])


def test_the_quote_form_is_prefilled_when_revising(client, rfq):
    token, _, _ = rfq
    client.post(f"/q/{token}/quote", data=QUOTE)
    html = client.get(f"/q/{token}/quote").text
    assert 'value="4800"' in html and "New 100mm slab" in html and 'value="7 years on workmanship"' not in html


def test_supplier_text_is_escaped_everywhere_it_is_shown_back(client, rfq):
    token, job_token, _ = rfq
    client.post(f"/q/{token}/quote", data={**QUOTE, "included": "<script>alert(1)</script>", "timeline": '"><img src=x onerror=alert(1)>'})
    for html in (client.get(f"/q/{token}/quote").text, client.get(f"/q/{token}?saved=1").text):
        assert "<script>alert(1)" not in html and "<img src=x" not in html


def test_long_text_is_capped_and_control_characters_dropped(client, rfq):
    token, _, ids = rfq
    client.post(f"/q/{token}/quote", data={**QUOTE, "notes": "x" * 5000, "timeline": "ok\x00\x07" + "y" * 400})
    q = stored(ids["rfq"])[1][0]
    assert len(q.summary) <= 500 and len(q.timeline_text) <= 200 and "\x00" not in q.timeline_text and "\x07" not in q.timeline_text


# ---- site visits and declines -------------------------------------------------------------------------------
def day(days_ahead: int = 7) -> str:
    return (timeutil.local_now() + datetime.timedelta(days=days_ahead)).date().isoformat()


def visit_data(**over):
    base = {"date1": day(7), "from1": "10:00", "to1": "12:00", "date2": day(8), "from2": "14:00", "to2": "16:30",
            "date3": "", "from3": "", "to3": "", "price": "4k to 6k", "notes": "Clear the car park"}
    return {**base, **over}


def test_a_site_visit_request_stores_real_dates_and_times(client, rfq):
    token, _, ids = rfq
    assert client.post(f"/q/{token}/visit", data=visit_data()).status_code == 303
    q = stored(ids["rfq"])[1][0]
    assert q.kind == "visit" and not q.is_quote and not q.windows_declined
    first, second = q.visit_windows
    assert first["start"] == f"{day(7)}T10:00" and first["end"] == f"{day(7)}T12:00"
    assert first["label"].endswith("10am to 12pm") and second["label"].endswith("2pm to 4:30pm")
    assert "Ballpark: 4k to 6k" in q.summary and "Clear the car park" in q.summary and "10am to 12pm" in q.summary


def test_three_options_duplicates_are_dropped(client, rfq):
    token, _, ids = rfq
    client.post(f"/q/{token}/visit", data=visit_data(date3=day(7), from3="10:00", to3="12:00"))  # same as option 1
    assert len(stored(ids["rfq"])[1][0].visit_windows) == 2


@pytest.mark.parametrize("over, message", [
    ({"date1": "", "from1": "", "to1": "", "date2": "", "from2": "", "to2": ""}, "at least one time"),
    ({"to1": ""}, "needs a date and both times"),
    ({"from1": "12:00", "to1": "10:00"}, "end time must be after"),
    ({"to1": "23:30", "from1": "09:00"}, "at most 12 hours"),
    ({"date1": day(-2)}, "in the past"),
    ({"date1": day(200)}, "more than 90 days"),
    ({"date1": "not-a-date"}, "valid date and time"),
])
def test_bad_visit_times_are_rejected_with_a_message_and_nothing_is_stored(client, rfq, over, message):
    token, _, ids = rfq
    r = client.post(f"/q/{token}/visit", data=visit_data(**over))
    assert r.status_code == 400 and message in r.text and stored(ids["rfq"]) == ([], [])


def test_a_decline_stores_the_reason_and_rejects_unknown_ones(client, rfq):
    token, _, ids = rfq
    assert client.post(f"/q/{token}/decline", data={"reason": "hacked"}).status_code == 400
    assert client.post(f"/q/{token}/decline", data={"reason": "out_of_area", "notes": "Only north of the river"}).status_code == 303
    q = stored(ids["rfq"])[1][0]
    assert q.kind == "decline" and q.decline_reason == "out_of_area" and not q.is_quote and "Only north of the river" in q.summary


# ---- limits --------------------------------------------------------------------------------------------------
def test_submissions_are_rate_limited(client, rfq):
    token, _, _ = rfq
    codes = [client.post(f"/q/{token}/decline", data={"reason": "other"}).status_code for _ in range(supplier_response.submit_limiter_ip.limit + 3)]
    assert codes[0] == 303 and codes[-1] == 429


# ---- what the customer sees ---------------------------------------------------------------------------------
def test_the_progress_page_shows_quotes_visits_and_declines_without_contact_details(client, rfq):
    token, job_token, ids = rfq
    db = get_session()
    for n, name in enumerate(("Beta Paving", "Gamma Slabs")):
        c = Company(job_id=ids["job"], place_id=f"extra-{n}-{secrets.token_hex(3)}", name=name, discovered_email=f"{name[:4].lower()}@secret.example")
        db.add(c)
        db.flush()
        db.add(RfqSend(job_id=ids["job"], company_id=c.id, reference_code=f"R{n}", recipient_email=c.discovered_email, subject="s", body="b",
                       status="sent", response_token=secrets.token_urlsafe(16)))
    db.commit()
    other = {r.company_id: r.response_token for r in db.query(RfqSend).filter_by(job_id=ids["job"]).all()}
    names = {c.name: c.id for c in db.query(Company).filter_by(job_id=ids["job"]).all()}
    db.close()

    client.post(f"/q/{token}/quote", data={**QUOTE, "price_type": "range", "price": "4800", "price_max": "5600"})
    client.post(f"/q/{other[names['Beta Paving']]}/visit", data=visit_data(date2="", from2="", to2=""))
    client.post(f"/q/{other[names['Gamma Slabs']]}/decline", data={"reason": "out_of_area"})

    page = client.get(f"/j/{job_token}").text
    assert "1 quote(s) received" in page and "1 want to visit" in page and "1 declined" in page
    assert "$4,800 to $5,600" in page and "Could visit: " in page and "10am to 12pm" in page and "Choose a time" in page and "Outside their area" in page
    assert "acme@example.com" not in page and "secret.example" not in page  # suppliers' details stay private


# ---- the email carries the link --------------------------------------------------------------------------
def test_the_rfq_email_offers_the_one_minute_response_link_only_when_there_is_one():
    job = SimpleNamespace(description="Replace driveway.", location_text="Fitzroy", budget_min=None, budget_max=None, deadline=None,
                          project_type=None, timeframe=None, ref_prefix="AB12CD34", category="concreter", title="Driveway")
    acme = SimpleNamespace(name="Acme")
    with_link = build_body(job, acme, "REF-1", "Irene", [], "https://quoteschmote.com/q/abc123")
    assert "https://quoteschmote.com/q/abc123" in with_link and "Or just reply to this email." in with_link
    assert with_link.index("/q/abc123") < with_link.index("Please keep this reference code")
    assert "/q/" not in build_body(job, acme, "REF-1", "Irene", [])
    assert "https://x.test/q/t" in build_rfq(job, acme, "Irene", [], "https://x.test/q/t")[2]
