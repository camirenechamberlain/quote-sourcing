"""The emails sent to suppliers when the customer manages their request: closes it, chooses someone, or adds details.
Pure text builders: nothing here sends or touches the database."""
from __future__ import annotations

from services.quote_format import money


def _sign(customer) -> str:
    return f"Quoteschmote, on behalf of {customer.name}" if customer and customer.name else "Quoteschmote"


def closed_for_supplier(job, company, customer) -> tuple[str, str]:
    body = f"""Hi {company.name} team,

A quick note: the customer has closed their {job.category} request for {job.location_text} (ref {job.ref_prefix}). Nothing more is needed from you, and any booked site visit has been cancelled.

Thanks for your time.
{_sign(customer)}
"""
    return f"Request closed: {job.title} [Ref: {job.ref_prefix}]", body


def awarded_to_supplier(job, company, customer, price: float | None, supplier_url: str | None) -> tuple[str, str]:
    name = customer.name if customer and customer.name else "The customer"
    price_line = f"  Agreed price: {money(price)}\n" if price else ""
    link = f"\nYour request page: {supplier_url}\n" if supplier_url else ""
    body = f"""Hi {company.name} team,

Good news: {name} has chosen you for the {job.category} job at {job.location_text} (ref {job.ref_prefix}).

{price_line}  Name:   {name}
  Mobile: {customer.mobile if customer and customer.mobile else "not provided"}
  Email:  {customer.email if customer and customer.email else "not provided"}
  Where:  {job.location_text}

Please contact them to confirm the details and arrange a start date.
{link}
Thanks,
{_sign(customer)}
"""
    return f"You've been chosen: {job.title} [Ref: {job.ref_prefix}]", body


def not_chosen_for_supplier(job, company, customer) -> tuple[str, str]:
    body = f"""Hi {company.name} team,

Thanks for taking the time to respond to the {job.category} request for {job.location_text} (ref {job.ref_prefix}). The customer has decided to go with another supplier this time. Any booked site visit has been cancelled.

We appreciate it, and hope to send more work your way.
{_sign(customer)}
"""
    return f"Update on {job.title} [Ref: {job.ref_prefix}]", body


def update_for_supplier(job, company, customer, note: str, new_files: list[str], supplier_url: str | None) -> tuple[str, str]:
    files = f"\nNew files attached: {', '.join(new_files)}\n" if new_files else ""
    link = f"\nIf this changes your quote, you can update it here: {supplier_url}\n" if supplier_url else ""
    said = f"\n{note}\n" if note else ""
    body = f"""Hi {company.name} team,

The customer has added some details to their {job.category} request for {job.location_text} (ref {job.ref_prefix}):
{said}{files}{link}
Thanks,
{_sign(customer)}
"""
    return f"Update to the request: {job.title} [Ref: {job.ref_prefix}]", body
