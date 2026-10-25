"""The emails (and calendar invites) around a site visit. Pure text builders: nothing here sends or touches the database."""
from __future__ import annotations

from services import timeutil
from services.ics import build_ics


def visit_invite(visit, job, company, *, for_supplier: bool, customer_first_name: str | None = None, cancelled: bool = False) -> tuple[str, str, bytes]:
    """(filename, mime type, bytes) calendar invite for a confirmed visit."""
    start, end = timeutil.parse_local(visit.start_local), timeutil.parse_local(visit.end_local)
    if for_supplier:
        summary = f"Site visit: {job.title}" + (f" ({customer_first_name})" if customer_first_name else "")
    else:
        summary = f"Site visit: {company.name} for {job.title}"
    data = build_ics(visit.id, start, end, summary, description=f"Quote request ref {job.ref_prefix}", location=job.location_text,
                     cancelled=cancelled)
    return "site-visit.ics", "text/calendar", data  # the METHOD (publish or cancel) is inside the file


def visit_confirmed_for_supplier(job, company, customer, visit, supplier_url: str | None) -> tuple[str, str]:
    link = f"\nChange or follow up here: {supplier_url}\n" if supplier_url else ""
    name = customer.name if customer and customer.name else "the customer"
    body = f"""Hi {company.name} team,

{name} has confirmed a site visit for the job below:

  When:   {visit.label}
  Where:  {job.location_text}
  Job:    {job.title} ({job.category}), ref {job.ref_prefix}
  Name:   {name}
  Mobile: {customer.mobile if customer and customer.mobile else "not provided"}
  Email:  {customer.email if customer and customer.email else "not provided"}

A calendar invite is attached. Once you've seen the job, please send your quote through the same link so it reaches the customer straight away.
{link}
Thanks,
Quoteschmote, on behalf of {name}
"""
    return f"Site visit confirmed: {job.title}, {visit.label} [Ref: {job.ref_prefix}]", body


def visit_confirmed_for_customer(job, company, visit, progress_url: str | None) -> tuple[str, str]:
    link = f"\nSee everything in one place: {progress_url}\n" if progress_url else ""
    body = f"""Hi,

Your site visit is booked:

  Who:   {company.name}
  When:  {visit.label}
  Where: {job.location_text}

A calendar invite is attached. {company.name} now has your first name, mobile and the job address for this visit, and nothing else is shared.
{link}
Quoteschmote
"""
    return f"Your site visit is booked: {company.name}, {visit.label}", body


def windows_declined_for_supplier(job, company, supplier_url: str | None) -> tuple[str, str]:
    link = f"\nOffer new times here (about a minute): {supplier_url}\n" if supplier_url else ""
    body = f"""Hi {company.name} team,

The customer can't make any of the times you offered for the {job.category} job at {job.location_text} (ref {job.ref_prefix}).
Could you offer a couple of other times?
{link}
Thanks,
Quoteschmote
"""
    return f"Could you offer other visit times? {job.title} [Ref: {job.ref_prefix}]", body


def visit_cancelled_for_supplier(job, company, visit, supplier_url: str | None) -> tuple[str, str]:
    link = f"\nOffer new times here: {supplier_url}\n" if supplier_url else ""
    body = f"""Hi {company.name} team,

The customer needs to cancel the site visit on {visit.label} for the {job.category} job at {job.location_text} (ref {job.ref_prefix}).
The calendar entry is cancelled. If you're still keen, please offer new times.
{link}
Thanks,
Quoteschmote
"""
    return f"Site visit cancelled: {job.title}, {visit.label} [Ref: {job.ref_prefix}]", body
