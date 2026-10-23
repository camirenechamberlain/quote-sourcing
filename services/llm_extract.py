from __future__ import annotations

import json
import os

import anthropic
from pydantic import BaseModel, ValidationError

MODEL = "claude-haiku-5-5"

QUOTE_TOOL = {
    "name": "record_quote",
    "description": "Record structured data extracted from a tradesperson's reply to a request-for-quote email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_quote": {
                "type": "boolean",
                "description": "True if this email contains an actual quote/price offer. False for auto-replies, 'we don't service that area', requests for more info without a price, etc.",
            },
            "price_amount": {"type": ["number", "null"], "description": "The quoted price, or the low end of a range. Null if not stated."},
            "price_max": {"type": ["number", "null"], "description": "The high end of a price range, if given. Null otherwise."},
            "currency": {"type": ["string", "null"], "description": "ISO currency code if inferable, e.g. AUD, USD."},
            "price_type": {"type": "string", "enum": ["fixed", "range", "hourly", "unknown"]},
            "scope_included": {"type": "array", "items": {"type": "string"}, "description": "Bullet points of what the quote covers."},
            "exclusions": {"type": "array", "items": {"type": "string"}, "description": "Bullet points of what's explicitly excluded or would cost extra."},
            "timeline_text": {"type": ["string", "null"], "description": "Estimated timeline/availability, verbatim or summarized."},
            "warranty_notes": {"type": ["string", "null"], "description": "Any warranty/guarantee mentioned."},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"], "description": "Your confidence that the extracted fields are accurate and complete."},
            "summary": {"type": "string", "description": "One-line summary of this reply for a comparison table."},
        },
        "required": ["is_quote", "price_type", "scope_included", "exclusions", "confidence", "summary"],
    },
}

SYSTEM_PROMPT = (
    "You extract structured data from tradesperson quote-reply emails. "
    "Never invent numbers or details not present in the text. Use null for anything unstated. "
    "If the email is not actually a quote (e.g. an auto-reply, a decline, or a request for more information "
    "with no price given), set is_quote to false."
)


class QuoteExtraction(BaseModel):
    is_quote: bool
    price_amount: float | None = None
    price_max: float | None = None
    currency: str | None = None
    price_type: str = "unknown"
    scope_included: list[str] = []
    exclusions: list[str] = []
    timeline_text: str | None = None
    warranty_notes: str | None = None
    confidence: str = "low"
    summary: str = ""


class ExtractionResult(BaseModel):
    parsed: QuoteExtraction
    raw_response: str


def _client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment (.env)")
    return anthropic.Anthropic(api_key=api_key)


def extract_quote(job_description: str, job_budget_text: str, reply_body: str) -> ExtractionResult:
    client = _client()
    user_message = (
        f"Original job description:\n{job_description}\n\n"
        f"Budget context: {job_budget_text}\n\n"
        f"Tradesperson's reply email (verbatim):\n---\n{reply_body}\n---"
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[QUOTE_TOOL],
        tool_choice={"type": "tool", "name": "record_quote"},
        messages=[{"role": "user", "content": user_message}],
    )

    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    raw_response = json.dumps(tool_use.input) if tool_use else response.model_dump_json()

    if tool_use is None:
        return ExtractionResult(
            parsed=QuoteExtraction(is_quote=False, confidence="low", summary="Model did not return structured output."),
            raw_response=raw_response,
        )

    try:
        parsed = QuoteExtraction.model_validate(tool_use.input)
    except ValidationError as e:
        parsed = QuoteExtraction(is_quote=False, confidence="low", summary=f"Failed to validate model output: {e}")

    return ExtractionResult(parsed=parsed, raw_response=raw_response)
