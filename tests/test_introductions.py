"""The introduction fee: shown to suppliers, agreed to when they offer visit times, recorded once per supplier when their details are released,
waived if the customer cancels, and visible in the admin ledger."""
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import config
from db.models import Company, Introduction, Job, Notification, Quote, ReplyEmail, RfqSend, User
from db.session import get_session
from server import customer_visits, job_manage, main, supplier_response
from services import supplier_terms
from tests.test_supplier_response import day, visit_data


@pytest.fixture()
def client():
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def fresh_limits(monkeypatch):
    for limiter in (supplier_response.view_limiter, supplier_response.submit_limiter_ip, supplier_response.submit_limiter_token,
                    customer_visits.view_limiter, customer_visits.action_limiter, job_manage.view_limiter, job_manage.action_limiter):
        limiter.hits.clear()
    monkeypatch.setattr(config, "INTRODUCTION_FEE", 0.0)
    monkeypatch.setattr(config, "INTRODUCTION_FEE_LIST", 30.0)


@pytest.fixture()
def booking():
    """A request with one supplier who has been emailed. The customer has an email and mobile."""
    db = get_session()
    try:
        tag = secrets.token_hex(4)
        user = User(whatsapp_number=f"web:in{tag}", name="Irene", email=f"cust-{tag}@example.com", mobile=f"+6140{secrets.randbelow(10**7):07d}")
        db.add(user)
        db.flush()
        job = Job(title=f"Tile {tag}", description="d", category="tiler", location_text="Reservoir VIC", user_id=user.id, status="ACTIVE")
        db.add(job)
        db.flush()
        company = Company(job_id=job.id, place_id=f"p-{tag}", name=f"Acme {tag}", discovered_email=f"acme-{tag}@example.com")
        db.add(company)
        db.flush()
        rfq = RfqSend(job_id=job.id, company_id=company.id, reference_code="R", recipient_email=company.discovered_email, subject="Request", body="b",
                      status="sent", response_token=secrets.token_urlsafe(16))
        db.add(rfq)
        db.commit()
        return SimpleNamespace(job=job.id, public=job.public_token, rfq=rfq.id, company=company.id, token=rfq.response_token, tag=tag)
    finally:
        db.close()


def offer(client, w, agree=True, **over):
    data = visit_data(**over)
    if not agree:
        data.pop("agree")
    return client.post(f"/q/{w.token}/visit", data=data)


def my_quote(w):
    db = get_session()
    try:
        reply = db.query(ReplyEmail).filter_by(rfq_send_id=w.rfq).first()
        return db.query(Quote).filter_by(reply_email_id=reply.id).first() if reply else None
    finally:
        db.close()


def book(client, w, slot="0"):
    q = my_quote(w)
    return client.post(f"/j/{w.public}/visit/{q.id}", data={"slot": slot, "consent": "1"})


def intros(w):
    db = get_session()
    try:
        return db.query(Introduction).filter_by(job_id=w.job).all()
    finally:
        db.close()


# ---- what suppliers are shown and agree to ---------------------------------------------------------------------------------------
def test_the_visit_form_describes_the_fee_and_asks_for_agreement(client, booking):
    page = client.get(f"/q/{booking.token}/visit").text
    assert "Introduction fee" in page and "Free during our beta. Normally an introduction fee of $30" in page
    assert "name='agree'" in page and "/supplier-terms" in page and "Quoting and responding are always free" in page


def test_once_charging_starts_the_form_states_the_amount(client, booking, monkeypatch):
    monkeypatch.setattr(config, "INTRODUCTION_FEE", 25.0)
    page = client.get(f"/q/{booking.token}/visit").text
    assert "An introduction fee of $25 applies only if the customer confirms a site visit" in page and "Free during our beta" not in page


def test_offering_times_requires_ticking_the_agreement_and_stores_nothing_otherwise(client, booking):
    r = offer(client, booking, agree=False)
    assert r.status_code == 400 and "tick the box to agree" in r.text and my_quote(booking) is None
    assert f'value="{day(7)}"' in r.text and 'value="4k to 6k"' in r.text  # what they typed is kept, so they only need to tick the box


def test_agreeing_is_recorded_with_the_terms_version(client, booking):
    assert offer(client, booking).status_code == 303
    q = my_quote(booking)
    assert q.fee_accepted_at is not None and q.fee_terms_version == supplier_terms.TERMS_VERSION


def test_the_terms_page_states_the_fee_the_beta_and_that_responding_is_free(client):
    page = client.get("/supplier-terms").text
    assert "Supplier terms" in page and supplier_terms.TERMS_VERSION in page and "draft" in page.lower()
    assert "never pay to receive a request" in page and "Free during our beta" in page and "waive the fee" in page


# ---- recording introductions -----------------------------------------------------------------------------------------------------------
def test_booking_a_visit_records_one_introduction_free_during_the_beta(client, booking):
    offer(client, booking)
    assert book(client, booking).status_code == 303
    (intro,) = intros(booking)
    assert intro.status == "free_beta" and intro.fee_cents == 0 and intro.list_fee_cents == 3000  # what it would have earned is still recorded
    assert intro.trigger == "visit_booked" and intro.accepted_at is not None and intro.terms_version == supplier_terms.TERMS_VERSION
    assert intro.company_id == booking.company and intro.rfq_send_id == booking.rfq


def test_when_charging_the_introduction_is_due_for_the_configured_amount(client, booking, monkeypatch):
    monkeypatch.setattr(config, "INTRODUCTION_FEE", 25.0)
    offer(client, booking)
    book(client, booking)
    (intro,) = intros(booking)
    assert intro.status == "due" and intro.fee_cents == 2500 and intro.list_fee_cents == 3000


def test_booking_a_different_time_never_creates_a_second_introduction(client, booking):
    offer(client, booking)
    book(client, booking, "0")
    book(client, booking, "1")
    assert len(intros(booking)) == 1


def test_cancelling_the_visit_waives_the_fee_and_booking_again_reinstates_the_same_record(client, booking):
    offer(client, booking)
    book(client, booking)
    (before,) = intros(booking)
    client.post(f"/j/{booking.public}/visit/{my_quote(booking).id}/cancel")
    (voided,) = intros(booking)
    assert voided.id == before.id and voided.status == "void" and "cancelled the visit" in voided.note
    offer(client, booking)  # the supplier offers fresh times after the customer cancelled
    book(client, booking)
    (back,) = intros(booking)
    assert back.id == before.id and back.status == "free_beta" and back.note is None


def test_a_paid_or_invoiced_introduction_is_never_voided_automatically(client, booking):
    offer(client, booking)
    book(client, booking)
    db = get_session()
    db.query(Introduction).filter_by(job_id=booking.job).one().status = "paid"
    db.commit()
    db.close()
    client.post(f"/j/{booking.public}/visit/{my_quote(booking).id}/cancel")
    assert intros(booking)[0].status == "paid"


def test_a_visit_offered_before_fees_existed_is_recorded_but_waived(client, booking):
    db = get_session()
    supplier_response.save_response(db, db.get(RfqSend, booking.rfq), db.get(Job, booking.job), db.get(Company, booking.company), kind="visit",
                                    visit_windows=supplier_response.parse_slots(visit_data())[0])  # no fee_accepted: an old-style offer
    db.close()
    book(client, booking)
    (intro,) = intros(booking)
    assert intro.status == "waived" and intro.fee_cents == 0 and intro.accepted_at is None and "not accepted" in intro.note


def test_closing_the_request_waives_introductions_for_visits_that_will_not_happen(client, booking):
    offer(client, booking)
    book(client, booking)
    client.post(f"/j/{booking.public}/close", data={"reason": "other"})
    assert intros(booking)[0].status == "void"


# ---- awarding ----------------------------------------------------------------------------------------------------------------------------
def quote_response(w):
    db = get_session()
    q = supplier_response.save_response(db, db.get(RfqSend, w.rfq), db.get(Job, w.job), db.get(Company, w.company), kind="quote", price_type="fixed", price=900.0)
    qid = q.id
    db.close()
    return qid


def test_choosing_a_supplier_without_a_visit_records_an_awarded_introduction_with_no_fee(client, booking):
    qid = quote_response(booking)
    client.post(f"/j/{booking.public}/choose/{qid}", data={"price": "900", "consent": "1"})
    (intro,) = intros(booking)
    assert intro.trigger == "awarded" and intro.status == "waived" and intro.fee_cents == 0 and intro.list_fee_cents == 3000


def test_choosing_after_a_booked_visit_does_not_add_a_second_introduction(client, booking):
    offer(client, booking)
    book(client, booking)
    db = get_session()  # the supplier then sends a price (their response is replaced by the quote)
    supplier_response.save_response(db, db.get(RfqSend, booking.rfq), db.get(Job, booking.job), db.get(Company, booking.company), kind="quote",
                                    price_type="fixed", price=900.0)
    db.close()
    client.post(f"/j/{booking.public}/choose/{my_quote(booking).id}", data={"price": "900", "consent": "1"})
    (intro,) = intros(booking)
    assert intro.trigger == "visit_booked" and intro.status == "free_beta"


# ---- what the supplier is told at booking ----------------------------------------------------------------------------------------------
def test_the_booking_email_states_the_fee_position(client, booking, monkeypatch):
    offer(client, booking)
    book(client, booking)
    mail = [n for n in get_session().query(Notification).filter_by(job_id=booking.job, kind="visit_confirmed").all()]
    assert len(mail) == 1 and "Introduction fee for this booking: none (free during our beta)." in mail[0].body

    monkeypatch.setattr(config, "INTRODUCTION_FEE", 25.0)
    assert "Introduction fee for this booking: $25 (we'll invoice you separately)." in supplier_terms.fee_status_line()


# ---- the admin ledger -------------------------------------------------------------------------------------------------------------------
def test_the_ledger_page_shows_totals_and_each_introduction(client, booking):
    from streamlit.testing.v1 import AppTest

    offer(client, booking)
    book(client, booking)
    page = Path(__file__).resolve().parent.parent / "pages" / "8_Introductions.py"
    at = AppTest.from_file(str(page), default_timeout=60).run()
    assert not at.exception
    assert any("Free beta" in i.value for i in at.info)
    labels = {m.label: m.value for m in at.metric}
    assert {"Introductions", "Would-be revenue at list price", "Outstanding (due + invoiced)", "Paid"} <= set(labels)
    assert int(labels["Introductions"]) >= 1 and labels["Would-be revenue at list price"].startswith("$")
    assert any(b.label == "Save changes" for b in at.button)  # the table branch rendered (not the "no introductions yet" empty state)
