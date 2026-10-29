"""Suppliers learn the suburb, not the street, until a visit is booked (or they are chosen)."""
import datetime
import secrets
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from db.models import Company, Job, RfqSend, User, Visit
from db.session import get_session
from server import main, supplier_response
from services import followups, job_messages, timeutil, visit_messages
from services.location import FALLBACK, coarse_location, public_location
from services.rfq_template import build_body

STREET = "12 Brunswick St"
FULL = f"{STREET}, Fitzroy VIC 3065"


@pytest.mark.parametrize("text, expected", [
    ("12 Brunswick St, Fitzroy VIC 3065", "Fitzroy VIC 3065"),
    ("Reservoir VIC", "Reservoir VIC"),
    ("Brunswick 3056", "Brunswick 3056"),
    ("3056", "3056"),
    ("Fitzroy, VIC, 3065", "Fitzroy, VIC, 3065"),  # a postcode on its own is not a street number
    ("St Kilda VIC 3182", "St Kilda VIC 3182"),  # a suburb that starts with "St"
    ("Port Melbourne", "Port Melbourne"),
    ("Unit 4/12 Smith Rd, Brunswick", "Brunswick"),
    ("Lot 7 Smith Court, Doncaster East", "Doncaster East"),
    ("Level 3, 120 Collins Street, Melbourne VIC 3000", "Melbourne VIC 3000"),
    ("12 Brunswick St Fitzroy VIC 3065", "Fitzroy VIC 3065"),  # the whole address on one line
    ("Unit 4/12 Smith Rd Brunswick", "Brunswick"),
    ("123 Main St, Springfield, IL 62704", "Springfield, IL 62704"),
    ("27 Granville St\nGlenroy VIC 3046", "Glenroy VIC 3046"),
    ("5 Miranda Rd", ""),  # nothing safe to show
    ("", ""),
    (None, ""),
])
def test_addresses_are_reduced_to_the_suburb(text, expected):
    assert coarse_location(text) == expected


def test_when_nothing_safe_remains_suppliers_get_a_plain_fallback_not_the_street():
    assert public_location(SimpleNamespace(location_text="5 Miranda Rd")) == FALLBACK and "Miranda" not in FALLBACK
    assert public_location(SimpleNamespace(location_text=None)) == FALLBACK


def job_ns(**over):
    base = dict(description="Fix the tile.", location_text=FULL, budget_min=None, budget_max=None, deadline=None, project_type=None, timeframe=None,
                ref_prefix="AB12CD34", category="tiler", title="Tile")
    return SimpleNamespace(**{**base, **over})


# ---- emails ------------------------------------------------------------------------------------------------------------------
def test_the_request_email_names_the_suburb_only():
    body = build_body(job_ns(), SimpleNamespace(name="Acme"), "REF", "Irene")
    assert "Fitzroy VIC 3065" in body and STREET not in body and "Brunswick" not in body


def test_updates_closures_and_declined_times_and_cancellations_name_the_suburb_only():
    job, company, customer = job_ns(), SimpleNamespace(name="Acme"), SimpleNamespace(name="Irene", mobile="0400", email="i@example.com")
    visit = SimpleNamespace(label="Tue 10am")
    texts = [
        job_messages.closed_for_supplier(job, company, customer)[1],
        job_messages.not_chosen_for_supplier(job, company, customer)[1],
        job_messages.update_for_supplier(job, company, customer, "note", [], None)[1],
        visit_messages.windows_declined_for_supplier(job, company, None)[1],
        visit_messages.visit_cancelled_for_supplier(job, company, visit, None)[1],
    ]
    for text in texts:
        assert STREET not in text and "Fitzroy VIC 3065" in text


def test_the_full_address_is_released_when_the_supplier_is_chosen_or_a_visit_is_booked():
    job, company, customer = job_ns(), SimpleNamespace(name="Acme"), SimpleNamespace(name="Irene", mobile="0400", email="i@example.com")
    assert FULL in job_messages.awarded_to_supplier(job, company, customer, 4500.0, None)[1]
    visit = SimpleNamespace(label="Tue 10am", id="v1", start_local="2026-11-03T10:00", end_local="2026-11-03T12:00")
    assert FULL in visit_messages.visit_confirmed_for_supplier(job, company, customer, visit, None)[1]
    assert b"12 Brunswick St\, Fitzroy VIC 3065" in visit_messages.visit_invite(visit, job, company, for_supplier=True)[2]  # (commas are escaped in .ics files)


# ---- the supplier's page and the chase email -----------------------------------------------------------------------------------
@pytest.fixture()
def client():
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def fresh_limits():
    for limiter in (supplier_response.view_limiter, supplier_response.submit_limiter_ip, supplier_response.submit_limiter_token):
        limiter.hits.clear()


@pytest.fixture()
def request_with_street_address():
    db = get_session()
    try:
        tag = secrets.token_hex(4)
        user = User(whatsapp_number=f"web:lc{tag}", name="Irene", email=f"cust-{tag}@example.com", mobile=f"+6140{secrets.randbelow(10**7):07d}")
        db.add(user)
        db.flush()
        job = Job(title=f"Tile {tag}", description="Fix the tile.", category="tiler", location_text=FULL, user_id=user.id, status="ACTIVE")
        db.add(job)
        db.flush()
        company = Company(job_id=job.id, place_id=f"p-{tag}", name=f"Acme {tag}", discovered_email=f"acme-{tag}@example.com")
        db.add(company)
        db.flush()
        token = secrets.token_urlsafe(16)
        rfq = RfqSend(job_id=job.id, company_id=company.id, reference_code="R", recipient_email=company.discovered_email, subject="Request", body="b",
                      status="sent", sent_at=datetime.datetime(2026, 11, 1), response_token=token)
        db.add(rfq)
        db.commit()
        return SimpleNamespace(job=job.id, rfq=rfq.id, company=company.id, token=token, mobile=user.mobile)
    finally:
        db.close()


def test_the_supplier_page_shows_the_suburb_and_says_when_the_rest_is_shared(client, request_with_street_address):
    page = client.get(f"/q/{request_with_street_address.token}").text
    assert "Fitzroy VIC 3065" in page and STREET not in page and "Brunswick" not in page
    assert "exact address and the customer's phone are shared when they confirm a visit" in page


def test_once_a_visit_is_booked_the_supplier_page_shows_the_full_address(client, request_with_street_address):
    w = request_with_street_address
    start = timeutil.local_now() + datetime.timedelta(days=3)
    db = get_session()
    db.add(Visit(job_id=w.job, rfq_send_id=w.rfq, company_id=w.company, start_local=timeutil.iso(start),
                 end_local=timeutil.iso(start + datetime.timedelta(hours=1)), label="Visit", status="confirmed"))
    db.commit()
    db.close()
    page = client.get(f"/q/{w.token}").text
    assert page.count(STREET) == 1 and w.mobile in page  # only in the booked-visit notice, with the customer's phone


def test_the_chase_email_names_the_suburb_only(request_with_street_address):
    w = request_with_street_address
    now = datetime.datetime(2026, 11, 5, 0, 0)  # four days after sending
    db = get_session()
    items = [p for p in followups.plan(db, now) if p.job_id == w.job]
    db.close()
    assert [p.kind for p in items] == ["chase_1"]
    assert "Fitzroy VIC 3065" in items[0].body and STREET not in items[0].body
