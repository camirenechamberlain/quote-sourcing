"""What a supplier may know about where the job is. Before a site visit is booked they only need the area to decide whether to quote, so
they get the suburb (and state or postcode), never the street address. The exact address is released with the customer's contact details
when the customer confirms a visit (or chooses the supplier).

This is a deterministic text rule, not a guess by a model, so it behaves the same every time and is easy to test."""
from __future__ import annotations

import re

_STREET_WORDS = (
    "st street rd road ave avenue av dr drive ct court pl place cres crescent hwy highway blvd boulevard way lane ln tce terrace pde parade "
    "cl close gr grove sq square esp esplanade cct circuit pkwy parkway rise row walk mews"
).split()
_STREET = "(?:" + "|".join(_STREET_WORDS) + ")"
_UNIT = r"(?:(?:unit|apt|apartment|flat|suite|lot|shop|level)\s*)?"
_NUMBER = r"\d+[a-z]?(?:\s*[/-]\s*\d+[a-z]?)?"

_PREFIX = re.compile(r"^(?:unit|apt|apartment|flat|suite|lot|shop|level|po box|p\.o\. box)\b", re.I)
_NUMBERED = re.compile(rf"^{_UNIT}{_NUMBER}\s+[a-z]", re.I)
_ENDS_STREET = re.compile(rf"\b{_STREET}\.?$", re.I)
_INLINE = re.compile(rf"^{_UNIT}{_NUMBER}\s+(?:[A-Za-z'.-]+\s+){{1,3}}?{_STREET}\b\.?,?\s*(.+)$", re.I)

FALLBACK = "the customer's area (exact address shared once a visit is booked)"


def _is_street(part: str) -> bool:
    text = part.strip()
    if _PREFIX.match(text):
        return True
    if _NUMBERED.match(text):
        words = text.split()
        if re.fullmatch(r"\d{4,5}", words[0]) and len(words) >= 2 and re.fullmatch(r"[A-Za-z]{2,3}", words[1].strip(".,")):
            return False  # "3065 VIC": a postcode with its state, not a street number
        return True
    return bool(_ENDS_STREET.search(text)) and not any(ch.isdigit() for ch in text) and len(text.split()) <= 5


def coarse_location(text: str | None) -> str:
    """The suburb part of an address: '12 Brunswick St, Fitzroy VIC 3065' -> 'Fitzroy VIC 3065'. Empty if nothing safe remains."""
    raw = (text or "").strip()
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"[,;\n]", raw) if p.strip()]
    kept = [p for p in parts if not _is_street(p)]
    if kept:
        return ", ".join(kept)
    match = _INLINE.match(raw)  # a whole address on one line: "12 Brunswick St Fitzroy VIC 3065"
    return match.group(1).strip(" ,") if match else ""


def public_location(job) -> str:
    """What to show a supplier before a visit is booked."""
    return coarse_location(getattr(job, "location_text", None)) or FALLBACK
