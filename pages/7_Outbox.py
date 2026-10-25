from __future__ import annotations

import pandas as pd
import streamlit as st

import config
from branding import inject_branding, brand_header
from db.models import Notification, Suppression
from db.session import get_session
from services import followups, outbound, timeutil

st.set_page_config(page_title="Outbox", page_icon="📤", layout="wide")
inject_branding()
brand_header("Outbox and follow-ups")

session = get_session()
mode = outbound.mode()

if mode == "dry_run":
    st.info("**Dry run.** Follow-ups are only previewed; nothing is emailed. Set `OUTBOUND_MODE=test` or `live` in `.env` to send.")
elif mode == "test":
    st.warning(f"**Test mode.** Every email goes to **{outbound.test_recipient() or '(set TEST_RECIPIENT)'}** instead of the real recipient.")
else:
    st.error("**LIVE.** Follow-ups go to real suppliers and customers.")

for warning in followups.config_warnings():
    st.warning(warning)

cols = st.columns(3)
cols[0].metric("Automatic follow-ups", "ON" if config.FOLLOWUPS_ENABLED else "OFF", help="Set FOLLOWUPS_ENABLED=1 in .env, then restart the site")
cols[1].metric("Runs every", f"{config.FOLLOWUP_INTERVAL_MINUTES} min")
local = timeutil.local_now()
cols[2].metric("Sending hours", "open now" if followups.in_business_hours(local) else "closed now",
               help=f"{followups.FIRST_HOUR}:00 to {followups.LAST_HOUR}:00 {config.TIMEZONE}, Monday to Saturday")

st.subheader("Due right now")
due = followups.plan(session)
if not due:
    st.success("Nothing is due.")
else:
    st.dataframe(
        pd.DataFrame([{"For": d.audience, "Rule": d.kind, "To": d.to_email, "Subject": d.subject} for d in due]),
        hide_index=True, use_container_width=True,
    )
    for d in due[:10]:
        with st.expander(f"{d.kind}: {d.subject}"):
            st.text(d.body)
    if st.button(f"Run follow-ups now ({'preview only in dry run' if mode == 'dry_run' else 'sends ' + str(len(due))})", type="primary"):
        report = followups.run(session, send=True)
        st.write(f"Sent {report.sent} of {len(report.planned)} due. {report.note}")
        st.rerun()

st.subheader("Recent emails")
rows = session.query(Notification).order_by(Notification.created_at.desc()).limit(200).all()
if rows:
    st.dataframe(
        pd.DataFrame([{"When (UTC)": n.created_at, "Status": n.status, "For": n.audience, "Kind": n.kind, "To": n.to_email,
                       "Subject": n.subject, "Error": n.error_message or ""} for n in rows]),
        hide_index=True, use_container_width=True,
    )
else:
    st.caption("Nothing has been recorded yet.")

st.subheader("Opted out")
opted = session.query(Suppression).order_by(Suppression.created_at.desc()).all()
if opted:
    st.dataframe(pd.DataFrame([{"Email": s.email, "Reason": s.reason, "When (UTC)": s.created_at} for s in opted]), hide_index=True, use_container_width=True)
else:
    st.caption("No supplier has opted out.")

session.close()
