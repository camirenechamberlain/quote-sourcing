"""The supplier terms and the wording of the introduction fee, in one place so every page and email says the same thing.

IMPORTANT: these are DRAFT terms written to describe how the product behaves. Have a lawyer review them (GST treatment, invoicing,
consumer and privacy law in each country you operate in) before you charge anyone real money."""
from __future__ import annotations

import config

TERMS_VERSION = "2026-10-29-draft"


def fee_text() -> str:
    """How the introduction fee is described to a supplier, given the current settings."""
    fee, listed = config.INTRODUCTION_FEE, config.INTRODUCTION_FEE_LIST
    if fee > 0:
        return (f"An introduction fee of ${fee:g} applies only if the customer confirms a site visit with you (once per request). "
                "Quoting and responding are always free.")
    if listed > 0:
        return (f"Free during our beta. Normally an introduction fee of ${listed:g} applies, only if the customer confirms a site visit "
                "with you (once per request). Quoting and responding are always free.")
    return "There is no fee. Quoting and responding are always free."


def fee_status_line() -> str:
    """One line for a booking confirmation email."""
    fee = config.INTRODUCTION_FEE
    return f"Introduction fee for this booking: ${fee:g} (we'll invoice you separately)." if fee > 0 else "Introduction fee for this booking: none (free during our beta)."


TERMS_SECTIONS = (
    ("Responding is free", "You never pay to receive a request, to quote, to offer visit times, or to decline."),
    ("The introduction fee", fee_text),
    ("When it applies", "The fee applies once per request, when the customer confirms a site visit with you or chooses you for the job. "
                        "If the customer cancels a visit and doesn't rebook with you, we waive the fee."),
    ("Customer details", "Until a visit is booked you see the job, the photos and the suburb. When the customer confirms, you receive their "
                         "first name, mobile, email and the exact address, to arrange that visit or job only. Please don't use them to market "
                         "to the customer or share them with anyone else."),
    ("Invoicing", "[DRAFT: confirm] Fees are invoiced after the visit is booked and are payable within 14 days. GST treatment to be confirmed."),
    ("Changes", "We may change the fee with at least 14 days' notice by email. You can stop receiving requests at any time using the "
                "link in any email."),
)
