from __future__ import annotations

import datetime
import time
from pathlib import Path

import streamlit as st

from branding import inject_branding, brand_header
from db.models import AppSetting, Company, Job, RfqSend
from db.session import get_session
from services import attachments as attachments_service
from services import gmail_service
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

sender_setting = session.get(AppSetting, "sender_name")
sender_name = sender_setting.value if sender_setting and sender_setting.value else "the requester"
if sender_name == "the requester":
    st.warning("You haven't set your name in **Settings** yet — emails will be signed generically until you do.")

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
    rs.company_id for rs in session.query(RfqSend).filter_by(job_id=job.id, status="sent").all()
}

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
        ref, subject, body = build_rfq(job, c, sender_name)
        if job.attachment_paths:
            n = len(job.attachment_paths)
            body += f"\n(I've attached {n} photo{'s' if n != 1 else ''} of the job for reference.)\n"
        st.session_state.rfq_drafts[c.id] = {
            "select": True,
            "email": c.discovered_email,
            "reference_code": ref,
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
st.markdown(f"### {len(to_send)} email(s) selected to send")

if st.button(f"Send Approved ({len(to_send)})", type="primary", disabled=not to_send):
    results = []
    progress = st.progress(0.0)
    for i, (company, draft) in enumerate(to_send):
        rfq_send = RfqSend(
            job_id=job.id,
            company_id=company.id,
            reference_code=draft["reference_code"],
            recipient_email=draft["email"],
            subject=draft["subject"],
            body=draft["body"],
            status="draft",
        )
        session.add(rfq_send)
        try:
            session.commit()
        except Exception:
            session.rollback()
            results.append((company.name, "skipped (already sent to this company for this job)"))
            progress.progress((i + 1) / len(to_send))
            continue

        try:
            message_id, thread_id = gmail_service.send_email(
                draft["email"], draft["subject"], draft["body"], attachment_paths=job.attachment_paths
            )
            rfq_send.gmail_message_id = message_id
            rfq_send.gmail_thread_id = thread_id
            rfq_send.status = "sent"
            rfq_send.sent_at = datetime.datetime.utcnow()
            results.append((company.name, "sent"))
        except GmailNotConnected as e:
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
        if outcome == "sent":
            st.success(f"{name}: sent")
        else:
            st.error(f"{name}: {outcome}")

    del st.session_state.rfq_drafts
    st.rerun()

session.close()
