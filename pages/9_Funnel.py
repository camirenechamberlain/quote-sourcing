from __future__ import annotations

import datetime

import pandas as pd
import streamlit as st

from branding import inject_branding, brand_header
from db.session import get_session
from services import funnel

st.set_page_config(page_title="Funnel", page_icon="📈", layout="wide")
inject_branding()
brand_header("Funnel")

RANGES = {"Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90, "All time": None}
left, right = st.columns([2, 3])
choice = left.selectbox("Period", list(RANGES), index=1)
include_test = right.checkbox("Include test sends", value=False, help="Jobs whose only emails were test-mode sends never reached a supplier.")
days = RANGES[choice]
since = datetime.datetime.utcnow() - datetime.timedelta(days=days) if days else None

session = get_session()
f = funnel.compute(session, since=since, include_test=include_test)
session.close()

if f.requests == 0:
    st.info("No requests in this period yet. The funnel fills in as customers make requests and suppliers respond.")
    st.stop()


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


cols = st.columns(4)
cols[0].metric("Requests", f.requests)
cols[1].metric("Supplier response rate", pct(f.response_rate), help="Suppliers who replied (quote, visit offer, decline or email) out of those emailed")
cols[2].metric("Quotes per job", "n/a" if f.quotes_per_contacted_job is None else f"{f.quotes_per_contacted_job:.1f}", help="Quotes received per request that reached suppliers")
cols[3].metric("Award rate", pct(f.award_rate), help="Requests where the customer chose a supplier, out of those that reached suppliers")

cols = st.columns(4)
cols[0].metric("Suppliers contacted", f.suppliers_contacted)
cols[1].metric("Visits booked", f.visits_booked, help="Each is an introduction, the event a fee would attach to")
cols[2].metric("Median time to first reply", "n/a" if f.median_hours_to_first_response is None else f"{f.median_hours_to_first_response:.1f} h")
cols[3].metric("Average agreed price", "n/a" if f.avg_agreed_price is None else f"${f.avg_agreed_price:,.0f}")

st.subheader("Where requests drop off")
steps = f.steps()
st.bar_chart(pd.DataFrame({"Requests reaching this stage": [n for _, n in steps]}, index=[label for label, _ in steps]))
table = pd.DataFrame(
    [{"Stage": label, "Requests": n, "% of requests": f"{n / f.requests * 100:.0f}%"} for label, n in steps]
)
st.dataframe(table, hide_index=True, use_container_width=True)

st.subheader("What suppliers said")
cols = st.columns(3)
cols[0].metric("Quotes", f.quotes)
cols[1].metric("Asked to visit first", f.visit_offers)
cols[2].metric("Declined", f.declines)
if f.decline_reasons:
    labels = {"out_of_area": "Outside their area", "too_busy": "Too busy", "wrong_trade": "Not their type of work", "other": "Another reason"}
    st.dataframe(pd.DataFrame([{"Reason": labels.get(k, k), "Suppliers": v} for k, v in sorted(f.decline_reasons.items(), key=lambda kv: -kv[1])]),
                 hide_index=True, use_container_width=True)

st.caption("A request is one a customer made on the website; jobs entered by hand are ignored. 'Shortlist approved' counts customer approvals only, so "
           "requests you sent yourself without approval appear under 'Suppliers contacted' but not under 'Shortlist approved'.")
