"""The customer approves (or trims) the supplier shortlist before anything is sent, and the operator's send page respects that."""
import datetime
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from db.models import Company, Job, RfqSend, User
from db.session import get_session
from server import job_manage, main
from services import followups, outbound
from services.job_state import summarise

H = datetime.timedelta
NOW = datetime.datetime(2026, 11, 3, 23, 0)  # 10:00 Wednesday in Melbourne (inside sending hours)


@pytest.fixture()
def client():
    return TestClient(main.app, follow_redirects=False)


@pytest.fixture(autouse=True)
def fresh_limits():
    for limiter in (job_manage.view_limiter, job_manage.action_limiter):
        limiter.hits.clear()


@pytest.fixture()
def shortlist_job():
    """A customer's request with three discovered suppliers (two with emails, one without) and nothing sent yet."""
    db = get_session()
    try:
        tag = secrets.token_hex(4)
        user = User(whatsapp_number=f"web:sl{tag}", name="Irene", email=f"cust-{tag}@example.com", mobile=f"+6140{secrets.randbelow(10**7):07d}")
        db.add(user)
        db.flush()
        job = Job(title=f"Tile {tag}", description="d", category="tiler", location_text="Reservoir VIC", user_id=user.id, status="QUEUED")
        db.add(job)
        db.flush()
        ids = {}
        for key, email, rating in (("A", f"a-{tag}@example.com", 4.9), ("B", f"b-{tag}@example.com", 5.0), ("N", None, 4.8)):
            c = Company(job_id=job.id, place_id=f"p-{key}-{tag}", name=f"Supplier {key} {tag}", address=f"{key} Street, Reservoir VIC, Australia", rating=rating,
                        discovered_email=email, scrape_status="success" if email else "no_email_found")
            db.add(c)
            db.flush()
            ids[key] = c.id
        db.commit()
        return SimpleNamespace(job=job.id, public=job.public_token, ids=ids, tag=tag, customer=user.email, title=job.title)
    finally:
        db.close()


def reload(model, ident):
    db = get_session()
    try:
        return db.get(model, ident)
    finally:
        db.close()


# ---- the panel -----------------------------------------------------------------------------------------------------------
def test_the_customer_sees_the_suppliers_we_found_with_nothing_sent_until_they_approve(client, shortlist_job):
    w = shortlist_job
    page = client.get(f"/j/{w.public}").text
    assert "Suppliers we plan to contact" in page and "Nothing is sent until you approve" in page and "Approve and send" in page
    assert f"Supplier A {w.tag}" in page and f"Supplier B {w.tag}" in page and "★ 4.9" in page and "A Street, Reservoir VIC" in page
    assert "★ 5.0" in page and ", Australia" not in page  # ratings keep their decimal, and the redundant country is dropped
    assert f"Supplier N {w.tag}" not in page  # we can't contact them (no email), so there's nothing to approve
    assert page.count("type='checkbox' name='keep'") == 2 and page.count(" checked>") == 2  # everyone starts ticked
    assert f"a-{w.tag}@example.com" not in page  # supplier emails are never shown
    assert "Review your supplier shortlist" in page


def test_supplier_names_in_the_panel_are_escaped(client, shortlist_job):
    w = shortlist_job
    db = get_session()
    db.get(Company, w.ids["A"]).name = "<script>alert(1)</script>"
    db.commit()
    db.close()
    page = client.get(f"/j/{w.public}").text
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page


def test_the_status_label_follows_the_stages():
    # summarise() labels: shortlist waiting -> approved -> contacted
    db = get_session()
    try:
        tag = secrets.token_hex(4)
        job = Job(title="t", description="d", category="tiler", location_text="x", status="QUEUED")
        db.add(job)
        db.flush()
        assert summarise(db, job).label == "Finding suppliers"
        db.add(Company(job_id=job.id, place_id=f"p{tag}", name="S", discovered_email=f"s{tag}@example.com"))
        db.flush()
        assert summarise(db, job).label == "Review your supplier shortlist"
        job.shortlist_approved_at = datetime.datetime.utcnow()
        assert summarise(db, job).label == "Approved: sending to suppliers"
        company = db.query(Company).filter_by(job_id=job.id).one()
        db.add(RfqSend(job_id=job.id, company_id=company.id, reference_code="R", recipient_email=company.discovered_email, subject="s", body="b", status="sent"))
        db.flush()
        assert summarise(db, job).label == "Waiting on 1 supplier"
    finally:
        db.rollback()
        db.close()


# ---- approving -------------------------------------------------------------------------------------------------------------
def test_approving_keeps_the_ticked_suppliers_and_removes_the_rest_for_good(client, shortlist_job):
    w = shortlist_job
    r = client.post(f"/j/{w.public}/shortlist", data={"keep": [w.ids["A"]]})
    assert r.status_code == 303 and r.headers["location"] == f"/j/{w.public}?approved=1"
    a, b = reload(Company, w.ids["A"]), reload(Company, w.ids["B"])
    assert a.included and not a.customer_removed
    assert not b.included and b.customer_removed  # removed: never to be contacted
    assert reload(Job, w.job).shortlist_approved_at is not None
    page = client.get(r.headers["location"]).text
    assert "We'll contact the suppliers you approved" in page and "Approved" in page
    assert f"Supplier A {w.tag}" in page and f"Supplier B {w.tag}" not in page
    assert "Approve and send" not in page and "Approved: sending to suppliers" in page


def test_the_customer_must_keep_at_least_one_supplier(client, shortlist_job):
    w = shortlist_job
    r = client.post(f"/j/{w.public}/shortlist", data={})
    assert r.status_code == 303 and "note=" in r.headers["location"]
    assert "Choose at least one supplier" in client.get(r.headers["location"]).text
    assert reload(Job, w.job).shortlist_approved_at is None and reload(Company, w.ids["B"]).included


def test_unknown_or_other_jobs_supplier_ids_are_ignored(client, shortlist_job):
    w = shortlist_job
    db = get_session()
    other_job = Job(title="Other", description="d", category="tiler", location_text="x", status="QUEUED")
    db.add(other_job)
    db.flush()
    foreign = Company(job_id=other_job.id, place_id=f"f{secrets.token_hex(4)}", name="Foreign", discovered_email="f@example.com")
    db.add(foreign)
    db.commit()
    foreign_id = foreign.id
    db.close()
    client.post(f"/j/{w.public}/shortlist", data={"keep": [w.ids["A"], "nonsense", foreign_id]})
    assert reload(Company, foreign_id).included and not reload(Company, foreign_id).customer_removed  # another job's supplier untouched
    assert reload(Company, w.ids["A"]).included


def test_suppliers_already_contacted_cannot_be_removed_from_the_list(client, shortlist_job):
    w = shortlist_job
    db = get_session()
    db.add(RfqSend(job_id=w.job, company_id=w.ids["A"], reference_code="R", recipient_email=f"a-{w.tag}@example.com", subject="s", body="b", status="sent"))
    db.commit()
    db.close()
    client.post(f"/j/{w.public}/shortlist", data={"keep": [w.ids["B"]]})
    a = reload(Company, w.ids["A"])
    assert a.included and not a.customer_removed  # already emailed: the shortlist only covers who is still waiting


def test_an_ended_request_cannot_be_approved(client, shortlist_job):
    w = shortlist_job
    client.post(f"/j/{w.public}/close", data={"reason": "other"})
    assert "This request has ended" in client.post(f"/j/{w.public}/shortlist", data={"keep": [w.ids["A"]]}).text
    assert reload(Job, w.job).shortlist_approved_at is None
    assert "Suppliers we plan to contact" not in client.get(f"/j/{w.public}").text


def test_the_dashboard_shows_the_shortlist_as_waiting_for_the_customer(client, shortlist_job, monkeypatch):
    from server import account

    w = shortlist_job
    db = get_session()
    html = account._job_card(db, db.get(Job, w.job))
    db.close()
    assert "Review your supplier shortlist" in html


# ---- the operator's send page -------------------------------------------------------------------------------------------------
def admin_page(w):
    from streamlit.testing.v1 import AppTest

    page = Path(__file__).resolve().parent.parent / "pages" / "3_Review_and_Send.py"
    at = AppTest.from_file(str(page), default_timeout=90).run()
    label = f"{w.title} (tiler, Reservoir VIC)"
    at.selectbox[0].select(label).run()
    assert not at.exception
    return at


def send_button(at):
    return next(b for b in at.button if b.label.startswith(("Preview", "Send")))


def test_the_send_page_waits_for_the_customers_approval_unless_the_operator_overrides(shortlist_job):
    at = admin_page(shortlist_job)
    assert any("hasn't approved the supplier shortlist" in w.value for w in at.warning)
    assert send_button(at).disabled  # blocked until approved or overridden
    at.checkbox(key="skip_approval").check().run()
    assert not send_button(at).disabled


def test_the_send_page_lists_only_the_suppliers_the_customer_kept(client, shortlist_job):
    w = shortlist_job
    client.post(f"/j/{w.public}/shortlist", data={"keep": [w.ids["A"]]})
    at = admin_page(w)
    assert any("approved the shortlist" in s.value for s in at.success)
    listed = [t.value for t in at.text_input if "@" in (t.value or "")]
    assert listed == [f"a-{w.tag}@example.com"]  # B was removed by the customer
    assert not send_button(at).disabled


def test_the_send_page_refuses_to_send_for_an_ended_request(client, shortlist_job):
    w = shortlist_job
    client.post(f"/j/{w.public}/shortlist", data={"keep": [w.ids["A"]]})
    client.post(f"/j/{w.public}/close", data={"reason": "other"})
    at = admin_page(w)
    assert any("has closed this request" in e.value for e in at.error)
    assert send_button(at).disabled


# ---- the "your shortlist is ready" email --------------------------------------------------------------------------------------
def plan_for(w, now=NOW):
    db = get_session()
    try:
        return [p for p in followups.plan(db, now) if p.job_id == w.job]
    finally:
        db.close()


def test_the_customer_is_told_once_that_their_shortlist_is_ready(client, shortlist_job, monkeypatch):
    w = shortlist_job
    items = plan_for(w)
    assert [p.kind for p in items] == ["shortlist_ready"]
    mail = items[0]
    assert mail.to_email == w.customer and "Review the list and approve it" in mail.body and "nothing is sent until you approve" in mail.body
    assert f"https://example.test/j/{w.public}" in mail.body and "2 suppliers" in mail.body
    sent = []
    monkeypatch.setattr(outbound, "mode", lambda: "live")
    monkeypatch.setattr(outbound, "deliver", lambda to, s, b, attachment_paths=None, extra_files=None: sent.append(to) or outbound.Delivery(True, "m", "t", to, False))
    db = get_session()
    followups.run(db, send=True, now=NOW)
    followups.run(db, send=True, now=NOW + H(hours=1))
    db.close()
    assert sent.count(w.customer) == 1  # once, however often it runs


@pytest.mark.parametrize("setup", ["approved", "closed", "already_sent", "no_email", "nothing_contactable"])
def test_no_shortlist_email_when_there_is_nothing_to_approve(shortlist_job, setup):
    w = shortlist_job
    db = get_session()
    job = db.get(Job, w.job)
    if setup == "approved":
        job.shortlist_approved_at = datetime.datetime.utcnow()
    elif setup == "closed":
        job.status = "CLOSED"
    elif setup == "already_sent":
        db.add(RfqSend(job_id=w.job, company_id=w.ids["A"], reference_code="R", recipient_email="x@example.com", subject="s", body="b", status="sent"))
    elif setup == "no_email":
        db.get(User, job.user_id).email = None
    else:
        for key in ("A", "B"):
            db.get(Company, w.ids[key]).discovered_email = None
    db.commit()
    db.close()
    assert "shortlist_ready" not in [p.kind for p in plan_for(w)]
