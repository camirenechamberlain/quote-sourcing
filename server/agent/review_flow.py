"""The supplier review as a short, tap-friendly conversation.

When a customer confirms a request, the supplier-eye reviewer decides which details a professional would genuinely need
to quote properly. Those questions (plus any sourcing preferences we don't know yet, plus a final "attach these" step) are
queued and asked ONE AT A TIME. Every question carries short options so the customer can tap an answer instead of typing,
and can always type their own, say "not sure", or send the request as it is.

When the customer doesn't know an answer (say, how wide their driveway is) there are two cheap ways out:
  * "Typical": a sensible value for a typical property, supplied by the reviewer in the call it already makes (no extra cost);
  * "Estimate from my photo": one small vision call, only when tapped, which the customer then confirms.
Both are recorded in the job brief as unconfirmed, so suppliers know to check on site.

Answers are recorded deterministically: sourcing preferences (project type, distance, rating, timeframe) set their own
fields, and every other answer becomes a labelled line in the job brief. No extra AI call is needed to record an answer.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

import config
from server.agent import photo_estimate, supplier_review

log = logging.getLogger("review_flow")

REQUIRED_FIELDS = ("category", "description", "location_text")

# Keys stored in the draft (underscore = internal, never shown to the reviewer or suppliers directly)
REVIEWED_FLAG = "_reviewed"  # the review runs once per request, never in a loop
QUEUE_KEY = "_queue"
DONE_KEY = "_qdone"
TOTAL_KEY = "_qtotal"
DETAILS_KEY = "_details"
ESTIMATES_KEY = "_estimates"  # how many photo estimates this request has used (a cost cap)

# After the questions have been asked, the customer may decline the rest and send the request as it is.
SKIP_REVIEW_WORDS = {"SEND AS IS", "SEND IT AS IS", "AS IS", "SEND ANYWAY", "JUST SEND IT"}
CONTINUE_WORDS = {
    "CONTINUE WITHOUT ATTACHING", "CONTINUE WITHOUT PHOTOS", "CONTINUE", "NO THANKS", "NOTHING TO ATTACH", "NO PHOTOS",
    "SKIP", "NONE", "NO", "YES", "OK", "OKAY",
}
PHOTOS_FIRST_STAGE = "photos_first"
ESTIMATE_WORDS = {"ESTIMATE FROM MY PHOTO", "ESTIMATE FROM MY PHOTOS", "ESTIMATE FROM PHOTO", "ESTIMATE FROM THE PHOTO", "ESTIMATE IT", "ESTIMATE", "GUESS", "GUESS FOR ME"}

ReviewFn = Callable[[dict, list], object]
EstimateFn = Callable[[dict, dict, list], "photo_estimate.Estimate"]

# Sourcing preferences have fixed, tappable options and are parsed into fields (not into the job brief).
PROJECT_OPTIONS = ["Residential", "Commercial"]
DISTANCE_OPTIONS = ["Within 5 km", "Within 10 km", "Within 25 km", "Anywhere"]
RATING_OPTIONS = ["4.5 stars or higher only", "Any rating is fine"]
TIMEFRAME_OPTIONS = ["ASAP", "Within 2 weeks", "Within a month", "Flexible"]
CONFIRM_OPTIONS = ["Yes, use that", "No, I'll enter it myself"]


def normalise_command(text: str) -> str:
    return re.sub(r"[^A-Za-z ]", "", text or "").strip().upper()


def _norm_option(text: str) -> str:
    """For matching a typed reply to an option label. Unlike normalise_command this KEEPS digits, otherwise
    'Within 5 km', 'Within 10 km' and 'Within 25 km' would all look the same."""
    return " ".join(re.sub(r"[^a-z0-9. ]", "", (text or "").lower()).split())


_UNSURE = re.compile(r"^(i )?(dont know|do not know|dunno|idk|not sure|unsure|no idea|skip|pass)\b")


def is_unsure(text: str) -> bool:
    """'Not sure', "I don't know", 'skip', 'no idea'...: the customer is declining to answer this one."""
    return bool(_UNSURE.match(normalise_command(text).lower()))


def is_typical_request(text: str) -> bool:
    n = normalise_command(text)
    return n == "TYPICAL" or n.startswith("USE TYPICAL")


def is_estimate_request(text: str) -> bool:
    return normalise_command(text) in ESTIMATE_WORDS


# ---- questions ---------------------------------------------------------------------------------------------
def as_question(q) -> dict:
    """Normalise a reviewer question (a dict, or a plain string from simple reviewers and tests)."""
    if isinstance(q, dict):
        options = [str(o).strip() for o in q.get("options") or [] if str(o).strip()][:6]
        return {
            "key": q.get("key"),
            "kind": q.get("kind", "pro"),
            "label": (q.get("label") or "").strip() or "Detail",
            "question": str(q.get("question") or "").strip(),
            "options": options,
            "multi": bool(q.get("multi")),
            "photo_estimable": bool(q.get("photo_estimable")),
            "typical": str(q.get("typical") or "").strip(),
            "stage": q.get("stage"),
        }
    return {
        "key": None, "kind": "pro", "label": "Detail", "question": str(q).strip(), "options": [], "multi": False,
        "photo_estimable": False, "typical": "", "stage": None,
    }


def preference_questions(draft: dict, needs_local: bool, project_type_matters: bool = False) -> list[dict]:
    """Sourcing preferences we still don't know. Anything the customer already told us is never asked again."""
    questions: list[dict] = []
    if project_type_matters and draft.get("project_type") not in ("residential", "commercial"):
        questions.append(as_question({
            "key": "project_type", "kind": "pref", "label": "Project type", "options": PROJECT_OPTIONS,
            "question": "Is this a residential or a commercial project? (It decides which type of supplier I look for.)",
        }))
    if needs_local and not draft.get("distance_preference"):
        where = draft.get("location_text") or "the job"
        questions.append(as_question({
            "key": "distance", "kind": "pref", "label": "Distance", "options": DISTANCE_OPTIONS,
            "question": f"How close do suppliers need to be to {where}?",
        }))
    if not draft.get("rating_preference"):
        questions.append(as_question({
            "key": "rating", "kind": "pref", "label": "Reviews", "options": RATING_OPTIONS,
            "question": "Do you only want suppliers rated 4.5 stars or higher on Google? (You'll get fewer options, but better reviewed ones.)",
        }))
    if not draft.get("timeframe"):
        questions.append(as_question({
            "key": "timeframe", "kind": "pref", "label": "When", "options": TIMEFRAME_OPTIONS,
            "question": "When does the job need to be done?",
        }))
    return questions


def attach_question(suggestions: list[str]) -> dict:
    bullets = "\n".join(f"- {s}" for s in suggestions)
    return as_question({
        "kind": "attach", "label": "Attachments", "options": ["Continue without attaching"],
        "question": f"It would also help suppliers quote accurately if you can attach:\n{bullets}",
    })


def photos_first_question() -> dict:
    """The very first step: photos, before any questions, so the reviewer can answer what it sees instead of asking."""
    return as_question({
        "kind": "attach", "label": "Photos", "stage": PHOTOS_FIRST_STAGE, "options": ["Continue without photos"],
        "question": (
            "Before any questions: attach a few photos of the job. I'll study them and only ask about what I can't "
            "tell from them. If you can, include something for scale, like a car or a door."
        ),
    })


def build_queue(draft: dict, gaps: list, needs_local: bool, project_type_matters: bool, suggestions: list[str]) -> list[dict]:
    queue = [q for q in (as_question(g) for g in gaps) if q["question"]]
    queue += preference_questions(draft, needs_local, project_type_matters)
    if suggestions:
        queue.append(attach_question(suggestions))
    return queue


def current_question(session) -> dict | None:
    queue = (session.draft or {}).get(QUEUE_KEY) or []
    return queue[0] if queue else None


def current_choices(session) -> dict | None:
    """What the website should render as tappable answers for the question being asked (None if no question is open)."""
    q = current_question(session)
    if not q:
        return None
    photos = bool(photo_estimate.image_paths(session.photo_paths))
    if q.get("proposal"):  # an estimate is waiting for the customer to confirm it
        return {"kind": "confirm", "label": q["label"], "options": list(CONFIRM_OPTIONS), "multi": False,
                "typical": None, "photo_estimable": False, "photos_attached": photos}
    return {
        "kind": q["kind"], "label": q["label"], "options": list(q["options"]), "multi": q["multi"],
        "typical": q.get("typical") or None,
        "photo_estimable": bool(q.get("photo_estimable")) and not q.get("tried_estimate"),
        "photos_attached": photos,
    }


def format_question(q: dict, number: int, total: int, render_options: bool = True, first: bool = False) -> str:
    """The message text. The website shows options as buttons (render_options=False); WhatsApp gets a numbered list."""
    parts: list[str] = []
    if q.get("stage") == PHOTOS_FIRST_STAGE:
        return f"{q['question']}\n\nAttach photos with the paperclip, or continue without."
    if q["kind"] == "attach":
        lead = "Before I send this out: " if first else ""
        return f"{lead}{q['question']}\n\nAttach files with the paperclip, or continue without."
    if first:
        parts.append("Before I send this out, a few quick questions so suppliers can quote accurately. Tap an answer or type your own, and skip any you like.")
    head = f"{number} of {total}: " if total > 1 else ""
    parts.append(f"{head}{q['question']}")
    if render_options:
        lines: list[str] = []
        if q["options"]:
            listed = list(q["options"]) + ["Not sure"]
            lines.append("\n".join(f"{i}) {o}" for i, o in enumerate(listed, 1)))
            lines.append("Reply with a number, or type your own answer.")
        if q.get("typical"):
            lines.append(f'Not sure? Reply "typical" to use: {q["typical"]}.')
        if q.get("photo_estimable") and not q.get("tried_estimate"):
            lines.append("Or send a photo and I'll estimate it.")
        if lines:
            parts.append("\n\n".join(lines))
    return "\n\n".join(parts)


# ---- answers -----------------------------------------------------------------------------------------------
def resolve_option(text: str, options: list[str], multi: bool = False) -> str:
    """Map '2', 'within 10 km' or '1, 3' onto the option label(s); anything else is the customer's own words."""
    text = (text or "").strip()
    listed = list(options) + ["Not sure"]

    def one(token: str) -> str | None:
        token = token.strip()
        if token.isdigit() and 1 <= int(token) <= len(listed):
            return listed[int(token) - 1]
        for option in listed:
            if token and _norm_option(option) == _norm_option(token):
                return option
        return None

    direct = one(text)
    if direct:
        return direct
    if multi:
        tokens = [t for t in re.split(r"[,;&]|\band\b", text) if t.strip()]
        resolved = [one(t) for t in tokens]
        if tokens and all(resolved):
            return ", ".join(resolved)
    return text


def parse_project_type(answer: str) -> str | None:
    a = answer.lower()
    if "commercial" in a:
        return "commercial"
    if "residential" in a:
        return "residential"
    if re.search(r"\b(shop|office|warehouse|cafe|restaurant|retail|business|industrial|factory|clinic|salon|pub|bar)\b", a):
        return "commercial"
    if re.search(r"\b(house|home|unit|apartment|townhouse|flat|villa)\b", a):
        return "residential"
    return None


def parse_distance(answer: str) -> tuple[str | None, float | None]:
    a = answer.lower()
    if re.search(r"anywhere|any distance|doesn.?t matter|does not matter|no limit|no preference|don.?t mind|not fussed", a):
        return "anywhere", None
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:km|kms|kilomet)", a)
    if match:
        return "local", float(match.group(1))
    return None, None


def parse_rating(answer: str) -> str | None:
    a = answer.lower()
    if re.search(r"\bany\b|don.?t mind|doesn.?t matter|no preference|not fussed|\bno\b", a):
        return "no_preference"
    if re.search(r"4\.5|\btop\b|highest|\bbest\b|highly|5 star|five star|\byes\b", a):
        return "top_rated_only"
    return None


def apply_preference(draft: dict, key: str, answer: str) -> dict:
    """Set the matching draft field(s) from a preference answer. Unparseable answers change nothing (defaults apply)."""
    draft = dict(draft)
    if key == "project_type":
        parsed = parse_project_type(answer)
        if parsed:
            draft["project_type"] = parsed
    elif key == "distance":
        preference, km = parse_distance(answer)
        if preference:
            draft["distance_preference"] = preference
            if km:
                draft["max_distance_km"] = km
    elif key == "rating":
        parsed = parse_rating(answer)
        if parsed:
            draft["rating_preference"] = parsed
    elif key == "timeframe":
        draft["timeframe"] = answer.strip()
    return draft


def _record(draft: dict, q: dict, answer: str) -> dict:
    """Store an answer: preferences set their fields, everything else becomes a labelled line in the job brief."""
    if q.get("key"):
        return apply_preference(draft, q["key"], answer)
    draft = dict(draft)
    draft[DETAILS_KEY] = list(draft.get(DETAILS_KEY) or []) + [{"label": q["label"], "answer": answer}]
    return draft


def _next(session, draft: dict, queue: list, render_options: bool) -> tuple[list[str], bool]:
    draft[QUEUE_KEY] = queue
    session.draft = draft  # reassigned (not mutated) so SQLAlchemy sees the change
    if not queue:
        return [], True
    return [format_question(queue[0], int(draft.get(DONE_KEY, 0)) + 1, int(draft.get(TOTAL_KEY, len(queue))), render_options)], False


def _reask(session, draft: dict, queue: list, lead: str, render_options: bool) -> tuple[list[str], bool]:
    draft[QUEUE_KEY] = queue
    session.draft = draft
    q = queue[0]
    text = format_question(q, int(draft.get(DONE_KEY, 0)) + 1, int(draft.get(TOTAL_KEY, len(queue))), render_options)
    return [f"{lead}\n\n{text}" if lead else text], False


def _propose_estimate(session, draft, queue, q, render_options, estimate_fn) -> tuple[list[str], bool]:
    """One small vision call (only because the customer asked). The result is a PROPOSAL they confirm, never a silent answer."""
    used = int(draft.get(ESTIMATES_KEY, 0))
    photos = photo_estimate.image_paths(session.photo_paths)
    if not photos:
        return _reask(session, draft, queue, "I need a photo first. Tap the paperclip to attach one and I'll estimate it.", render_options)
    q["tried_estimate"] = True  # one attempt per question, however it goes
    queue[0] = q
    if used >= config.MAX_ESTIMATES_PER_REQUEST:
        return _reask(session, draft, queue, "I've used up my photo estimates for this request, so please choose or type an answer.", render_options)
    draft[ESTIMATES_KEY] = used + 1
    estimate_fn = estimate_fn or photo_estimate.estimate_from_photos  # resolved at call time so tests can patch it
    try:
        est = estimate_fn(q, _public_draft(draft), photos)
    except Exception:  # noqa: BLE001
        log.exception("Photo estimate raised")
        est = photo_estimate.Estimate(False, "", "", "low")
    if not (est.ok and est.answer):
        return _reask(
            session, draft, queue,
            "I couldn't work that out reliably from the photo (nothing in it gives a clear sense of scale).", render_options,
        )
    q["proposal"] = {"answer": est.answer, "basis": est.basis, "confidence": est.confidence}
    queue[0] = q
    draft[QUEUE_KEY] = queue
    session.draft = draft
    caution = "It's only a rough estimate" + (" and the photo angle makes it less certain" if est.confidence == "low" else "") + ", and suppliers will confirm on site."
    basis = f" ({est.basis.rstrip('.')})" if est.basis else ""
    message = f"From your photo, it looks {est.answer}{basis}. {caution} Does that look right?"
    if render_options:
        message += "\n\n" + "\n".join(f"{i}) {o}" for i, o in enumerate(CONFIRM_OPTIONS, 1)) + "\n\nReply with a number, or type the right answer."
    return [message], False


def _answer_proposal(session, draft, queue, q, text, render_options) -> tuple[list[str], bool]:
    proposal = q["proposal"]
    choice = resolve_option(text, CONFIRM_OPTIONS)
    cmd = normalise_command(choice)
    queue = list(queue)
    if choice == CONFIRM_OPTIONS[0] or cmd in {"YES", "Y", "YEP", "YEAH", "OK", "OKAY", "CORRECT", "RIGHT", "THATS RIGHT", "USE THAT"}:
        draft = _record(draft, q, f"{proposal['answer']} (estimated from photo, to be confirmed on site)")
        queue.pop(0)
        draft[DONE_KEY] = int(draft.get(DONE_KEY, 0)) + 1
        return _next(session, draft, queue, render_options)
    q = {k: v for k, v in q.items() if k != "proposal"}
    queue[0] = q
    if choice == CONFIRM_OPTIONS[1] or cmd in {"NO", "NOPE", "NAH"} or is_unsure(text):
        return _reask(session, draft, queue, "No problem.", render_options)
    # anything else is the customer giving the answer themselves
    draft = _record(draft, q, resolve_option(text, q["options"], q["multi"]))
    queue.pop(0)
    draft[DONE_KEY] = int(draft.get(DONE_KEY, 0)) + 1
    return _next(session, draft, queue, render_options)


def answer_current(
    session, text: str, new_attachments: list[str], render_options: bool = True, estimate_fn: EstimateFn | None = None,
    review_fn: ReviewFn | None = None,
) -> tuple[list[str], bool]:
    """Record the customer's reply to the open question and move on. Returns (replies, finished)."""
    draft = dict(session.draft or {})
    queue = list(draft.get(QUEUE_KEY) or [])
    q = dict(queue[0])
    queue[0] = q
    done, total = int(draft.get(DONE_KEY, 0)), int(draft.get(TOTAL_KEY, len(queue)))
    text = (text or "").strip()

    if not text and not new_attachments:  # nothing usable: ask again
        return _reask(session, draft, queue, "", render_options)

    if q.get("proposal"):
        return _answer_proposal(session, draft, queue, q, text, render_options)

    if q["kind"] == "attach":
        if text and not new_attachments and normalise_command(text) not in CONTINUE_WORDS and not is_unsure(text):
            draft = dict(draft)
            draft[DETAILS_KEY] = list(draft.get(DETAILS_KEY) or []) + [{"label": "Note", "answer": text}]
        queue.pop(0)
        if q.get("stage") == PHOTOS_FIRST_STAGE:  # photos are in (or declined): now the reviewer can look and decide what to ask
            draft[QUEUE_KEY] = []
            session.draft = draft
            replies = _analyse(session, draft, review_fn, render_options)
            return (replies, False) if replies else ([], True)
        return _next(session, draft, queue, render_options)

    # --- a normal question ---
    if not text:  # a file arrived while a question is open
        if q.get("photo_estimable") and not q.get("tried_estimate"):
            return _propose_estimate(session, draft, queue, q, render_options, estimate_fn)  # they sent it so we can estimate
        return _reask(session, draft, queue, "Got it, thanks.", render_options)

    if is_estimate_request(text):
        if not q.get("photo_estimable") or q.get("tried_estimate"):
            return _reask(session, draft, queue, "I can't estimate that one from a photo.", render_options)
        return _propose_estimate(session, draft, queue, q, render_options, estimate_fn)

    if is_typical_request(text) and q.get("typical"):
        draft = _record(draft, q, f"{q['typical']} (typical for this kind of property, not measured)")
    else:
        answer = resolve_option(text, q["options"], q["multi"])
        if answer and not is_unsure(answer):
            draft = _record(draft, q, answer)
    queue.pop(0)
    draft[DONE_KEY] = done + 1
    return _next(session, draft, queue, render_options)


# ---- starting, skipping and querying the review -----------------------------------------------------------------
def accepts_as_is(session, text: str) -> bool:
    """True if the customer wants the request sent exactly as it is, skipping any further questions."""
    if session is None or session.state not in ("collecting", "awaiting_confirm"):
        return False
    draft = session.draft or {}
    return normalise_command(text) in SKIP_REVIEW_WORDS and all(draft.get(f) for f in REQUIRED_FIELDS)


def accept_as_is(session) -> None:
    """Move to confirmation and mark the review as done, dropping any questions still queued."""
    draft = dict(session.draft or {})
    draft[REVIEWED_FLAG] = True
    draft[QUEUE_KEY] = []
    session.draft = draft
    session.state = "awaiting_confirm"


def review_pending(session) -> bool:
    return bool(session and session.state == "collecting" and (session.draft or {}).get(REVIEWED_FLAG))


def _public_draft(draft: dict) -> dict:
    return {k: v for k, v in (draft or {}).items() if not k.startswith("_")}


def run_supplier_review(session, review_fn: ReviewFn | None = None, render_options: bool = True) -> list[str] | None:
    """Read the finished request as a supplier would. Returns the FIRST question (the rest follow one at a time and the
    session is reopened for answers), or None if nothing needs asking. Runs at most once per request and fails open: if the
    reviewer errors, the customer is never blocked."""
    draft = dict(session.draft or {})
    if draft.get(REVIEWED_FLAG):
        return None
    draft[REVIEWED_FLAG] = True
    session.draft = draft  # reassigned (not mutated) so SQLAlchemy sees the change

    if config.PHOTOS_FIRST and not photo_estimate.image_paths(session.photo_paths):
        question = photos_first_question()  # ask for photos before any questions; the review runs once they're in
        draft[QUEUE_KEY] = [question]
        draft[DONE_KEY], draft[TOTAL_KEY] = 0, 0
        session.draft = draft
        session.state = "collecting"
        return [format_question(question, 1, 1, render_options)]
    return _analyse(session, draft, review_fn, render_options)


def _analyse(session, draft: dict, review_fn: ReviewFn | None, render_options: bool) -> list[str] | None:
    """The review itself (the reviewer sees the photos). Returns what to say first, or None if nothing needs asking."""
    review_fn = review_fn or supplier_review.review_request  # resolved at call time so tests can patch it
    try:
        result = review_fn(_public_draft(draft), list(session.photo_paths or []))
    except Exception:  # noqa: BLE001
        log.exception("Supplier review failed; proceeding without it")
        return None
    findings: list[tuple[str, str]] = []
    if isinstance(result, supplier_review.Review):
        gaps, needs_local = list(result.questions), result.needs_local_supplier
        project_type_matters, suggestions = result.project_type_matters, list(result.suggested_attachments)
        findings = list(result.photo_findings)
    else:  # a plain list of questions (simple reviewers and tests)
        gaps, needs_local, project_type_matters, suggestions = list(result or []), True, False, []

    # What the reviewer saw in the photos goes in the brief as unconfirmed, and the customer is told what it concluded.
    seen = ""
    if findings:
        draft[DETAILS_KEY] = list(draft.get(DETAILS_KEY) or []) + [
            {"label": label, "answer": f"{finding} (seen in photos, to be confirmed on site)"} for label, finding in findings
        ]
        seen = "I've looked at your photos. Here's what I can see:\n" + "\n".join(f"- {label}: {finding}" for label, finding in findings)

    # Work that can be done remotely has no proximity requirement, so don't ask how close suppliers must be.
    if not needs_local and not draft.get("distance_preference"):
        draft["distance_preference"] = "anywhere"

    queue = build_queue(draft, gaps, needs_local, project_type_matters, suggestions)
    if not queue:
        session.draft = draft
        return None

    draft[QUEUE_KEY] = queue
    draft[DONE_KEY] = 0
    draft[TOTAL_KEY] = sum(1 for q in queue if q["kind"] != "attach")
    draft.setdefault(DETAILS_KEY, [])
    session.draft = draft
    session.state = "collecting"
    question = format_question(queue[0], 1, draft[TOTAL_KEY], render_options, first=not seen)
    return [seen, question] if seen else [question]
