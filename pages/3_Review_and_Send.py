from __future__ import annotations

import datetime
import secrets
import time
from pathlib import Path

import streamlit as st

import config
from branding import inject_branding, brand_header
from db.models import AppSetting, Company, Job, RfqSend, User
from db.session import get_session
from services import attachments as attachments_service
from services import outbound
from services.gmail_service import GmailNotConnected
from services.rfq_template import build_rfq

st.set_page_config(page_title="Review and Send", page_icon="✉️", layout="wide")
inject_branding()
brand_header("Review and Send RFQs")

session = get_session()
jobs = session.query(Job).order_by(Job.created_at.desc()).all()

if not jobs:
    st.info("No jobs yet.")
    session.close()
    st.stop()

job_labels = {f"{j.title} ({j.category}, {j.location_text})": j.id for j in jobs}
selected_label = st.selectbox("Job", list(job_labels.keys()))
job_id = job_labels[selected_label]
job = session.get(Job, job_id)

# Emails are written on behalf of the customer, so they use the customer's first name (the Settings name is only a fallback
# for jobs entered by hand).
customer = session.get(User, job.user_id) if job.user_id else None
sender_setting = session.get(AppSetting, "sender_name")
sender_name = (customer.name if customer and customer.name else None) or (
    sender_setting.value if sender_setting and sender_setting.value else "the requester"
)
if sender_name == "the requester":
    st.warning("This job has no customer name and you haven't set one in **Settings**, so emails will be signed generically.")
else:
    st.caption(f"Emails are written on behalf of **{sender_name}**.")

mode = outbound.mode()
if mode == "dry_run":
    st.info("**Dry run.** Nothing will be emailed. You can review exactly what would go out. Set `OUTBOUND_MODE=test` or `live` in `.env` to send.")
elif mode == "test":
    if outbound.test_recipient():
        st.warning(f"**Test mode.** Every email goes to **{outbound.test_recipient()}** (not the suppliers), labelled with who it would have gone to.")
    else:
        st.error("**Test mode** needs `TEST_RECIPIENT` set to your own email address in `.env`. Nothing can be sent until it is.")
else:
    st.error("**LIVE.** These emails go to real businesses.")

if not config.PUBLIC_BASE_URL:
    st.warning("`PUBLIC_BASE_URL` isn't set in `.env` (for example `https://quoteschmote.com`), so these emails will NOT include the "
               "one-tap response link for suppliers. They can still reply by email.")

with st.expander(f"📎 Attachments ({len(job.attachment_paths or [])}) — sent with every RFQ for this job", expanded=True):
    current = job.attachment_paths or []
    if current:
        for path_str in list(current):
            name = Path(path_str).name.split("_", 1)[-1]  # strip the dedupe-uuid prefix for display
            cols = st.columns([4, 1])
            cols[0].write(f"🖼️ {name}" if Path(path_str).suffix.lower() != ".pdf" else f"📄 {name}")
            if cols[1].button("Remove", key=f"remove_attach_{path_str}"):
                attachments_service.delete_attachment(path_str)
                job.attachment_paths = [p for p in job.attachment_paths if p != path_str]
                session.commit()
                st.rerun()
    else:
        st.caption("No photos attached yet.")

    uploaded = st.file_uploader(
        "Add photos (PNG/JPG/GIF/WEBP) or PDFs — attached to every email sent for this job",
        type=["png", "jpg", "jpeg", "gif", "webp", "pdf"],
        accept_multiple_files=True,
        key=f"uploader_{job.id}",
    )
    if uploaded:
        new_paths = attachments_service.save_uploaded_files(job.id, uploaded)
        if new_paths:
            job.attachment_paths = (job.attachment_paths or []) + new_paths
            session.commit()
            st.rerun()

    total_bytes = attachments_service.total_size(job.attachment_paths or [])
    if total_bytes > attachments_service.MAX_TOTAL_BYTES:
        st.error(
            f"Attachments total {total_bytes / 1_000_000:.1f}MB, close to Gmail's per-message limit. "
            "Remove some before sending."
        )

companies = (
    session.query(Company)
    .filter_by(job_id=job.id, included=True)
    .filter(Company.discovered_email.isnot(None))
    .order_by(Company.name)
    .all()
)

already_sent_ids = {
    rs.company_id for rs in session.query(RfqSend).filter_by(job_id=job.id, status="sent", test_mode=False).all()
}  # test sends don't count: they never reached the supplier

if not companies:
    st.info("No eligible candidates yet (need an email + must be included). Go to **Discover Companies** first.")
    session.close()
    st.stop()

st.caption(f"{len(companies)} candidate(s) with an email on file. {len(already_sent_ids)} already sent for this job.")

if "rfq_drafts" not in st.session_state or st.session_state.get("rfq_drafts_job") != job.id:
    st.session_state.rfq_drafts_job = job.id
    st.session_state.rfq_drafts = {}
    for c in companies:
        if c.id in already_sent_ids:
            continue
        # Each request gets its own unguessable response link (needs PUBLIC_BASE_URL, e.g. https://quoteschmote.com).
        response_token = secrets.token_urlsafe(16)
        response_url = f"{config.PUBLIC_BASE_URL}/q/{response_token}" if config.PUBLIC_BASE_URL else None
        ref, subject, body = build_rfq(
            job, c, sender_name, attachments_service.supplier_file_names(job.attachment_paths), response_url
        )
        st.session_state.rfq_drafts[c.id] = {
            "select": True,
            "email": c.discovered_email,
            "reference_code": ref,
            "response_token": response_token,
            "subject": subject,
            "body": body,
        }

if st.button("Regenerate all drafts"):
    del st.session_state.rfq_drafts
    st.rerun()

to_send = []
for c in companies:
    if c.id in already_sent_ids:
        with st.container(border=True):
            st.markdown(f"**{c.name}** — `{c.discovered_email}`")
            st.caption("Already sent for this job.")
        continue

    draft = st.session_state.rfq_drafts.get(c.id)
    if draft is None:
        continue

    with st.container(border=True):
        cols = st.columns([0.5, 3, 1])
        draft["select"] = cols[0].checkbox("Send", value=draft["select"], key=f"select_{c.id}")
        cols[1].markdown(f"**{c.name}**")
        draft["email"] = cols[2].text_input("To", value=draft["email"], key=f"email_{c.id}", label_visibility="collapsed")
        with st.expander("Edit subject / body"):
            draft["subject"] = st.text_input("Subject", value=draft["subject"], key=f"subject_{c.id}")
            draft["body"] = st.text_area("Body", value=draft["body"], key=f"body_{c.id}", height=220)
        if draft["select"]:
            to_send.append((c, draft))

st.divider()
st.markdown(f"### {len(to_send)} email(s) selected")

acknowledged = True
if mode == "live":
    acknowledged = st.checkbox("I understand these will be emailed to real businesses", key="live_ack")
blocked = (mode == "test" and not outbound.test_recipient()) or not acknowledged
button_label = {
    "dry_run": f"Preview {len(to_send)} email(s) (nothing is sent)",
    "test": f"Send {len(to_send)} TEST email(s) to {outbound.test_recipient() or '(set TEST_RECIPIENT)'}",
    "live": f"Send {len(to_send)} email(s) to suppliers",
}[mode]

if st.button(button_label, type="primary", disabled=not to_send or blocked):
    n_files = len(job.attachment_paths or [])
    if mode == "dry_run":
        for company, draft in to_send:  # nothing is recorded, so a later real send is never blocked by a preview
            st.success(f"Would send to {draft['email']} ({company.name}) with {n_files} attachment(s).")
            with st.expander(f"{draft['subject']}"):
                st.text(draft["body"])
        st.stop()

    results = []
    progress = st.progress(0.0)
    for i, (company, draft) in enumerate(to_send):
        rfq_send = session.query(RfqSend).filter_by(job_id=job.id, company_id=company.id).first()
        if rfq_send and rfq_send.status == "sent" and not rfq_send.test_mode:
            results.append((company.name, "skipped (already sent to this company for this job)"))
            progress.progress((i + 1) / len(to_send))
            continue
        if rfq_send is None:  # a failed or test attempt is reused, so the one-RFQ-per-supplier rule never blocks a real send
            rfq_send = RfqSend(job_id=job.id, company_id=company.id, reference_code=draft["reference_code"],
                               recipient_email=draft["email"], subject=draft["subject"], body=draft["body"])
            session.add(rfq_send)
        rfq_send.reference_code, rfq_send.recipient_email = draft["reference_code"], draft["email"]
        rfq_send.response_token = draft.get("response_token")
        rfq_send.subject, rfq_send.body, rfq_send.status, rfq_send.error_message = draft["subject"], draft["body"], "draft", None
        session.commit()

        try:
            delivery = outbound.deliver(draft["email"], draft["subject"], draft["body"], attachment_paths=job.attachment_paths)
            rfq_send.gmail_message_id = delivery.message_id
            rfq_send.gmail_thread_id = delivery.thread_id
            rfq_send.status = "sent"
            rfq_send.test_mode = delivery.test_mode
            rfq_send.sent_at = datetime.datetime.utcnow()
            results.append((company.name, f"sent to {delivery.sent_to} (test)" if delivery.test_mode else "sent"))
        except (GmailNotConnected, outbound.OutboundError) as e:
            rfq_send.status = "failed"
            rfq_send.error_message = str(e)
            results.append((company.name, f"failed: {e}"))
        except Exception as e:  # noqa: BLE001
            rfq_send.status = "failed"
            rfq_send.error_message = str(e)[:500]
            results.append((company.name, f"failed: {e}"))
        session.commit()
        progress.progress((i + 1) / len(to_send))
        time.sleep(0.5)

    for name, outcome in results:
        if outcome.startswith("sent"):
            st.success(f"{name}: {outcome}")
        else:
            st.error(f"{name}: {outcome}")

    del st.session_state.rfq_drafts
    st.rerun()

session.close()
