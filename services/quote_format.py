"""Small formatting helpers shared by the supplier response page and the customer's progress page."""
from __future__ import annotations


def money(amount: float | None) -> str:
    if amount is None:
        return ""
    return f"${amount:,.0f}" if float(amount).is_integer() else f"${amount:,.2f}"


def describe_price(price_type: str | None, amount: float | None, high: float | None = None) -> str:
    """'$4,800', '$4,800 to $5,600' or '$95 per hour'."""
    if amount is None:
        return ""
    if price_type == "range" and high:
        return f"{money(amount)} to {money(high)}"
    if price_type == "hourly":
        return f"{money(amount)} per hour"
    return money(amount)
