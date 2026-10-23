"""Supplier filters driven by what the customer asked for."""
from __future__ import annotations


def apply_rating_filter(job, companies) -> int:
    """Exclude suppliers below the customer's minimum Google rating (and unrated ones, which can't meet it).

    Marks them `included=False` with a status/note explaining why, so they are never scraped or emailed.
    Returns how many were newly excluded. Does nothing when the customer has no rating preference.
    """
    minimum = getattr(job, "min_rating", None)
    if not minimum:
        return 0
    excluded = 0
    for company in companies:
        if company.rating is not None and company.rating >= minimum:
            continue
        if company.included:
            excluded += 1
        company.included = False
        company.scrape_status = "excluded_rating"
        company.scrape_notes = (
            "No Google rating" if company.rating is None else f"Rated {company.rating:g}, below the requested {minimum:g}"
        )
    return excluded
