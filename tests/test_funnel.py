"""The funnel numbers, checked against a scenario where every right answer is known."""
import datetime
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest

from db.models import Company, Introduction, Job, ReplyEmail, RfqSend, User
from db.session import get_session
from server import supplier_response
from services import funnel

H = datetime.timedelta
SENT = datetime.datetime(2026, 11, 1, 9, 0)


def make_job(db, *, approved=False, status="ACTIVE", price=None, created=None, with_user=True):
    tag = secrets.token_hex(4)
    user = None
    if with_user:
        user = User(whatsapp_number=f"web:fn{tag}", name="Irene", email=f"c-{tag}@example.com", mobile=f"+6140{secrets.randbelow(10**7):07d}")
        db.add(user)
        db.flush()
    job = Job(title=f"Job {tag}", description="d", category="tiler", location_text="Reservoir VIC", user_id=user.id if user else None, status=status,
              final_price=price, shortlist_approved_at=SENT if approved else None)
    if created:
        job.created_at = created
    db.add(job)
    db.flush()
    return job


def add_supplier(db, job, *, email=True, sent=False, test=False):
    tag = secrets.token_hex(4)
    company = Company(job_id=job.id, place_id=f"p-{tag}", name=f"S {tag}", discovered_email=f"s-{tag}@example.com" if email else None)
    db.add(company)
    db.flush()
    if not sent:
        return company, None
    rfq = RfqSend(job_id=job.id, company_id=company.id, reference_code="R", recipient_email=company.discovered_email or "x@example.com", subject="s",
                  body="b", status="sent", sent_at=SENT, test_mode=test, response_token=secrets.token_urlsafe(16))
    db.add(rfq)
    db.flush()
    return company, rfq


def respond(db, job, company, rfq, kind, **kw):
    supplier_response.save_response(db, rfq, job, company, kind=kind, **kw)


@pytest.fixture()
def scenario():
    """J1: approved, 3 suppliers emailed. A quoted (after 2h), B offered a visit that was booked (5h), C declined (3h); awarded to A at $4,500.
    J2: suppliers found, not approved, nothing sent. J3: a test job (only a test-mode send)."""
    db = get_session()
    try:
        j1 = make_job(db, approved=True, status="AWARDED", price=4500.0)
        a, rfq_a = add_supplier(db, j1, sent=True)
        b, rfq_b = add_supplier(db, j1, sent=True)
        c, rfq_c = add_supplier(db, j1, sent=True)
        j1.chosen_company_id = a.id
        db.commit()  # the supplier responses below make their own commits
        respond(db, j1, a, rfq_a, "quote", price_type="fixed", price=4800.0)
        respond(db, j1, b, rfq_b, "visit", visit_windows=[{"start": "2026-11-08T10:00", "end": "2026-11-08T12:00", "label": "x"}])
        respond(db, j1, c, rfq_c, "decline", decline_reason="out_of_area")
        # the responses were just made, so set when they arrived (2, 5 and 3 hours after sending) to make the response times known
        for rfq, hours in ((rfq_a, 2), (rfq_b, 5), (rfq_c, 3)):
            db.query(ReplyEmail).filter_by(rfq_send_id=rfq.id).one().received_at = SENT + H(hours=hours)
        db.add(Introduction(job_id=j1.id, rfq_send_id=rfq_b.id, company_id=b.id, trigger="visit_booked", status="free_beta", list_fee_cents=3000))
        j2 = make_job(db)
        add_supplier(db, j2)
        add_supplier(db, j2)
        j3 = make_job(db)
        add_supplier(db, j3, sent=True, test=True)
        db.commit()
        return SimpleNamespace(ids={j1.id, j2.id, j3.id}, j1=j1.id, j2=j2.id, j3=j3.id)
    finally:
        db.close()


def run(s, **kw):
    db = get_session()
    try:
        return funnel.compute(db, job_ids=s.ids, **kw)
    finally:
        db.close()


def test_every_number_matches_the_scenario(scenario):
    f = run(scenario)
    assert (f.requests, f.with_shortlist, f.approved, f.contacted_jobs) == (2, 2, 1, 1)  # the test job is left out
    assert (f.suppliers_contacted, f.suppliers_responded) == (3, 3) and f.response_rate == 1.0
    assert (f.quotes, f.visit_offers, f.declines) == (1, 1, 1) and f.decline_reasons == {"out_of_area": 1}
    assert (f.jobs_with_response, f.jobs_with_quote, f.visits_booked, f.jobs_with_visit) == (1, 1, 1, 1)
    assert (f.awarded, f.closed, f.award_rate, f.avg_agreed_price) == (1, 0, 1.0, 4500.0)
    assert f.quotes_per_contacted_job == 1.0 and f.median_hours_to_first_response == 3.0


def test_the_steps_for_the_chart_are_in_funnel_order(scenario):
    labels = [label for label, _ in run(scenario).steps()]
    assert labels == ["Requests made", "Suppliers found", "Shortlist approved", "Suppliers contacted", "Got a response", "Got a quote", "Visit booked", "Awarded"]


def test_test_sends_are_only_counted_when_asked_for(scenario):
    f = run(scenario, include_test=True)
    assert f.requests == 3 and f.contacted_jobs == 2 and f.suppliers_contacted == 4


def test_a_voided_introduction_is_not_a_booked_visit(scenario):
    db = get_session()
    db.query(Introduction).filter_by(job_id=scenario.j1).one().status = "void"
    db.commit()
    db.close()
    f = run(scenario)
    assert f.visits_booked == 0 and f.jobs_with_visit == 0


def test_only_requests_in_the_period_are_counted(scenario):
    db = get_session()
    db.get(Job, scenario.j2).created_at = datetime.datetime(2020, 1, 1)
    db.commit()
    db.close()
    f = run(scenario, since=datetime.datetime(2026, 1, 1))
    assert f.requests == 1 and f.with_shortlist == 1  # J2 is too old


def test_jobs_entered_by_hand_in_the_admin_are_ignored():
    db = get_session()
    try:
        job = make_job(db, with_user=False)
        add_supplier(db, job, sent=True)
        db.commit()
        assert funnel.compute(db, job_ids={job.id}).requests == 0
    finally:
        db.close()


def test_an_empty_funnel_has_no_rates_rather_than_dividing_by_zero():
    db = get_session()
    try:
        f = funnel.compute(db, job_ids=set())
        assert f.requests == 0 and f.response_rate is None and f.award_rate is None and f.quotes_per_contacted_job is None
        assert f.median_hours_to_first_response is None and f.avg_agreed_price is None
    finally:
        db.close()


def test_the_funnel_page_renders_with_data_and_with_none(scenario, monkeypatch):
    from streamlit.testing.v1 import AppTest

    real = funnel.compute
    only_here = lambda db, since=None, include_test=False: real(db, since, include_test, job_ids=scenario.ids)  # noqa: E731 - this test's jobs only
    monkeypatch.setattr(funnel, "compute", only_here)
    page = Path(__file__).resolve().parent.parent / "pages" / "9_Funnel.py"
    at = AppTest.from_file(str(page), default_timeout=60).run()
    assert not at.exception
    labels = {m.label: m.value for m in at.metric}
    assert {"Requests", "Supplier response rate", "Quotes per job", "Award rate", "Visits booked"} <= set(labels)
    assert labels["Requests"] == "2" and labels["Supplier response rate"] == "100%" and labels["Award rate"] == "100%" and labels["Visits booked"] == "1"

    monkeypatch.setattr(funnel, "compute", lambda db, since=None, include_test=False: real(db, since, include_test, job_ids=set()))
    empty = AppTest.from_file(str(page), default_timeout=60).run()
    assert not empty.exception and any("No requests in this period yet" in i.value for i in empty.info)
