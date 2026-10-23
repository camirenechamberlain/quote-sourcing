from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import streamlit as st

from branding import inject_branding, brand_header
from db.models import Company, Job
from db.session import get_session
from services import places_service
from services.filters import apply_rating_filter
from services.places_service import PlacesApiError
from services.scraping import scrape_company_email

st.set_page_config(page_title="Discover Companies", page_icon="🔍", layout="wide")
inject_branding()
brand_header("Discover Companies")

session = get_session()
jobs = session.query(Job).order_by(Job.created_at.desc()).all()

if not jobs:
    st.info("No jobs yet. Create one on the **New Job** page first.")
    session.close()
    st.stop()

job_labels = {f"{j.title} ({j.category}, {j.location_text})": j.id for j in jobs}
selected_label = st.selectbox("Job", list(job_labels.keys()))
job_id = job_labels[selected_label]
job = session.get(Job, job_id)

prefs = [f"Category: **{job.category}**", f"Location: **{job.location_text}**", f"Radius: **{job.radius_m / 1000:.0f} km**"]
if job.project_type:
    prefs.append(f"Project: **{job.project_type}**")
if job.min_rating:
    prefs.append(f"Customer wants **{job.min_rating:g}★+** suppliers only")
if job.timeframe:
    prefs.append(f"When: **{job.timeframe}**")
st.caption(" · ".join(prefs))

col_a, col_b = st.columns(2)

if col_a.button("Discover companies", type="primary"):
    try:
        with st.spinner("Geocoding location..."):
            if job.lat is None or job.lng is None:
                lat, lng = places_service.geocode(job.location_text)
                job.lat, job.lng = lat, lng
                session.commit()
            else:
                lat, lng = job.lat, job.lng

        with st.spinner("Searching Google Places..."):
            found = places_service.search_companies(places_service.build_query(job.category, job.project_type), lat, lng, job.radius_m)

        existing_place_ids = {c.place_id for c in session.query(Company).filter_by(job_id=job.id).all()}
        added = 0
        for item in found:
            if not item["place_id"] or item["place_id"] in existing_place_ids:
                continue
            session.add(
                Company(
                    job_id=job.id,
                    place_id=item["place_id"],
                    name=item["name"] or "Unknown",
                    address=item.get("address"),
                    scrape_status="pending",
                )
            )
            existing_place_ids.add(item["place_id"])
            added += 1
        session.commit()
        st.success(f"Found {len(found)} results, added {added} new candidates.")
    except PlacesApiError as e:
        st.error(str(e))
    except Exception as e:  # noqa: BLE001 - surface any API failure to the user
        st.error(f"Discovery failed: {e}")

companies = session.query(Company).filter_by(job_id=job.id).order_by(Company.name).all()

if col_b.button("Fetch details + scrape emails", disabled=not companies):
    targets = [c for c in companies if c.scrape_status == "pending"]

    # Fetch website/phone details first (sequential, cheap call count)
    with st.spinner(f"Fetching details for {len(targets)} companies..."):
        for c in targets:
            try:
                details = places_service.get_place_details(c.place_id)
                c.website = details.get("website")
                c.phone = details.get("phone")
                c.rating = details.get("rating")
            except Exception as e:  # noqa: BLE001
                c.scrape_notes = f"Details fetch failed: {e}"[:200]
        session.commit()

    # Honour the customer's rating preference before spending time scraping anyone.
    skipped = apply_rating_filter(job, targets)
    session.commit()
    targets = [c for c in targets if c.included]
    if skipped:
        st.info(f"Skipped {skipped} supplier(s) below the customer's {job.min_rating:g}-star preference.")

    progress = st.progress(0.0, text="Scraping company websites for contact emails...")
    results = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        future_to_company = {pool.submit(scrape_company_email, c.website): c.id for c in targets}
        done = 0
        for future in as_completed(future_to_company):
            company_id = future_to_company[future]
            results[company_id] = future.result()
            done += 1
            progress.progress(done / max(len(targets), 1))

    for c in targets:
        r = results.get(c.id)
        if r is None:
            continue
        c.discovered_email = r.email
        c.email_source = r.source
        c.scrape_status = r.status
        c.scrape_notes = r.notes
    session.commit()
    progress.empty()
    st.success(f"Scraped {len(targets)} companies.")
    companies = session.query(Company).filter_by(job_id=job.id).order_by(Company.name).all()

if companies:
    st.subheader(f"Candidates ({len(companies)})")

    df = pd.DataFrame(
        [
            {
                "id": c.id,
                "Include": c.included,
                "Name": c.name,
                "Address": c.address or "",
                "Phone": c.phone or "",
                "Website": c.website or "",
                "Rating": c.rating,
                "Email": c.discovered_email or "",
                "Email source": c.email_source,
                "Scrape status": c.scrape_status,
                "Notes": c.scrape_notes or "",
            }
            for c in companies
        ]
    )

    edited = st.data_editor(
        df,
        column_config={
            "id": None,
            "Include": st.column_config.CheckboxColumn(),
            "Email": st.column_config.TextColumn(help="Edit if scraping didn't find one"),
        },
        disabled=["Name", "Address", "Phone", "Website", "Rating", "Email source", "Scrape status", "Notes"],
        hide_index=True,
        use_container_width=True,
        key="candidate_editor",
    )

    if st.button("Save changes"):
        by_id = {c.id: c for c in companies}
        for _, row in edited.iterrows():
            c = by_id.get(row["id"])
            if c is None:
                continue
            c.included = bool(row["Include"])
            new_email = (row["Email"] or "").strip()
            if new_email and new_email != (c.discovered_email or ""):
                c.discovered_email = new_email
                c.email_source = "manual"
                c.scrape_status = "success"
        session.commit()
        st.success("Saved.")

    missing = sum(1 for c in companies if c.included and not c.discovered_email)
    if missing:
        st.warning(f"{missing} included candidate(s) have no email yet — add one manually or deselect them before sending.")
    st.caption("Next: go to **Review and Send** to draft and approve RFQ emails.")
else:
    st.info("No candidates yet — click **Discover companies** above.")

session.close()
