"""PDF attachments (e.g. engineering drawings): sanitising, limits, and what suppliers receive."""
import email
import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from db.models import ChatSession, Job
from db.session import get_session
from server import web_chat
from services import documents
from services.gmail_service import build_mime_message, display_name
from tests.test_web_chat import (  # reuse the website test helpers and fixtures
    client, contact, fake_model, fresh_limits, jpeg_bytes, refined_chat, say, start,
)

__all__ = ["client", "fake_model", "fresh_limits"]  # fixtures imported for pytest


def make_pdf(pages=1, nasty=False, password=None, size=(842, 595)) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=size[0], height=size[1])
    if nasty:
        w.add_metadata({"/Author": "Secret Author", "/Title": "Private Residence 12 Smith St"})
        w.add_js("app.alert('hi')")
        w.add_attachment("payload.txt", b"hidden")
    if password:
        w.encrypt(password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


# ---- the sanitiser -------------------------------------------------------------------------------------------
def test_nasty_pdf_is_rebuilt_without_scripts_embedded_files_or_metadata():
    raw = make_pdf(nasty=True)
    assert b"JavaScript" in raw and b"EmbeddedFile" in raw and b"Secret Author" in raw  # the input really is nasty

    clean = documents.sanitise_pdf(raw)
    for needle in (b"JavaScript", b"EmbeddedFile", b"Secret Author", b"Private Residence", b"OpenAction"):
        assert needle not in clean, needle
    reader = PdfReader(io.BytesIO(clean))
    assert len(reader.pages) == 1
    assert (float(reader.pages[0].mediabox.width), float(reader.pages[0].mediabox.height)) == (842.0, 595.0)  # page kept as-is
    assert dict(reader.metadata or {}) == {"/Producer": "Quoteschmote"}


def test_multi_page_drawing_keeps_every_page_and_size():
    clean = documents.sanitise_pdf(make_pdf(pages=5, size=(1190, 842)))  # A3 landscape sheets
    reader = PdfReader(io.BytesIO(clean))
    assert len(reader.pages) == 5 and float(reader.pages[2].mediabox.width) == 1190.0


def test_annotation_actions_are_removed_but_the_annotation_stays():
    from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, TextStringObject

    w = PdfWriter()
    page = w.add_blank_page(width=200, height=200)
    annot = DictionaryObject({
        NameObject("/Type"): NameObject("/Annot"),
        NameObject("/Subtype"): NameObject("/Link"),
        NameObject("/Rect"): ArrayObject([FloatObject(10), FloatObject(10), FloatObject(100), FloatObject(100)]),
        NameObject("/A"): DictionaryObject({
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject("https://evil.example/phish"),
        }),
    })
    page[NameObject("/Annots")] = ArrayObject([w._add_object(annot)])
    buf = io.BytesIO()
    w.write(buf)

    def first_annotation(data):  # parsed, because pypdf escapes characters inside strings so byte-searching can't work
        return PdfReader(io.BytesIO(data)).pages[0]["/Annots"][0].get_object()

    assert "/A" in first_annotation(buf.getvalue())  # the input really carries an action
    cleaned = first_annotation(documents.sanitise_pdf(buf.getvalue()))
    assert "/A" not in cleaned and "/AA" not in cleaned
    assert cleaned["/Subtype"] == "/Link"  # the annotation itself is kept


@pytest.mark.parametrize(
    "data, fragment",
    [
        (b"", "empty"),
        (b"just some text pretending to be a pdf", "isn't a PDF"),
        (b"%PDF-1.7\nthis is not a real pdf body", "couldn't be read"),
        (make_pdf(password="secret"), "password"),
        (make_pdf(pages=documents.MAX_PAGES + 1), "pages"),
    ],
)
def test_bad_pdfs_are_rejected_with_a_helpful_reason(data, fragment):
    with pytest.raises(documents.BadPdf) as exc:
        documents.sanitise_pdf(data)
    assert fragment in str(exc.value)


def test_oversize_pdf_rejected():
    with pytest.raises(documents.BadPdf):
        documents.sanitise_pdf(b"%PDF-1.4\n" + b"0" * (documents.MAX_PDF_BYTES + 1))


def test_type_is_decided_by_content_not_name():
    assert documents.is_pdf(make_pdf()) and not documents.is_pdf(jpeg_bytes())


def test_safe_filename_keeps_the_customers_name_but_nothing_odd():
    assert documents.safe_filename("Structural Drawings Rev B.pdf") == "Structural Drawings Rev B.pdf"
    assert documents.safe_filename("../../etc/passwd.pdf") == "passwd.pdf"  # directory parts dropped
    assert documents.safe_filename("../../etc/passwd<script>.pdf") == "passwdscript.pdf"  # odd characters dropped
    assert documents.safe_filename(None) == "Drawing.pdf"
    assert documents.safe_filename("???.pdf") == "Drawing.pdf"
    assert len(documents.safe_filename("x" * 300 + ".pdf")) <= 64


# ---- uploading through the website -----------------------------------------------------------------------------
def pdf_part(name="Structural Drawings.pdf", data=None):
    return ("files", (name, data or make_pdf(pages=2, nasty=True), "application/pdf"))


def test_pdf_upload_is_accepted_cleaned_and_reaches_the_job(client):
    token = refined_chat(client, files=[pdf_part()])
    assert say(client, token, "yes").json()["state"] == "need_details"
    assert contact(client, token).json()["state"] == "confirmed"

    db = get_session()
    try:
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(token)).one()
        job = db.query(Job).filter_by(user_id=chat.user_id).one()
        assert len(job.attachment_paths) == 1
        path = Path(job.attachment_paths[0])
        assert path.suffix == ".pdf" and path.name.endswith("_Structural Drawings.pdf")
        raw = path.read_bytes()
        assert b"JavaScript" not in raw and b"Secret Author" not in raw  # the stored copy is the cleaned one
        assert len(PdfReader(io.BytesIO(raw)).pages) == 2
    finally:
        db.close()


def test_summary_names_every_attachment_exactly_as_suppliers_will_see_it(client):
    token = start(client).json()["token"]
    files = [("files", ("a.jpg", jpeg_bytes(), "image/jpeg")), pdf_part()]
    reply = say(client, token, "plumber for a leaking tap in carlton", files=files).json()["replies"][0]
    assert "Attachments (sent to every supplier):" in reply
    assert "• photo-1.jpg" in reply and "• Structural Drawings.pdf" in reply  # no storage prefix, photos numbered


def test_a_pdf_with_the_wrong_name_or_type_is_still_judged_by_content(client):
    token = start(client).json()["token"]
    sneaky = ("files", ("holiday.jpg", make_pdf(), "image/jpeg"))  # a PDF pretending to be a jpg
    assert say(client, token, "plumber for a leaking tap in carlton", files=[sneaky]).status_code == 200
    fake = ("files", ("drawing.pdf", b"<script>alert(1)</script>", "application/pdf"))  # junk pretending to be a PDF
    r = say(client, token, "hi", files=[fake])
    assert r.status_code == 400 and r.json()["error"] == "bad_upload"


def test_password_protected_pdf_gets_a_clear_message(client):
    token = start(client).json()["token"]
    r = say(client, token, "hi", files=[pdf_part("locked.pdf", make_pdf(password="pw"))])
    assert r.status_code == 400 and "password" in r.json()["message"] and "locked.pdf" in r.json()["message"]


def stored_pdfs_for(token):
    db = get_session()
    try:
        chat = db.query(ChatSession).filter_by(token_hash=web_chat.hash_token(token)).one()
        folder = web_chat.attachments_service.ATTACHMENTS_DIR / "_incoming" / chat.user_id
    finally:
        db.close()
    return [p for p in folder.glob("*.pdf")] if folder.exists() else []


def test_too_many_pdfs_rejected_and_nothing_is_kept(client):
    token = start(client).json()["token"]
    files = [pdf_part(f"sheet{i}.pdf", make_pdf()) for i in range(web_chat.MAX_PDFS_PER_REQUEST + 1)]
    r = say(client, token, "hi", files=files)
    assert r.status_code == 400 and r.json()["error"] == "too_many_pdfs"
    assert stored_pdfs_for(token) == []  # the PDFs accepted before the limit was hit were cleaned up


def test_failed_upload_in_a_batch_discards_the_good_files_too(client):
    token = start(client).json()["token"]
    files = [pdf_part("good.pdf", make_pdf()), pdf_part("locked.pdf", make_pdf(password="pw"))]
    assert say(client, token, "hi", files=files).status_code == 400
    assert stored_pdfs_for(token) == []


def test_total_attachment_size_is_capped(client, monkeypatch):
    monkeypatch.setattr(web_chat.attachments_service, "MAX_TOTAL_BYTES", 2_000)
    token = start(client).json()["token"]
    r = say(client, token, "hi", files=[pdf_part("big.pdf", make_pdf(pages=30))])
    assert r.status_code == 400 and r.json()["error"] == "too_large"


# ---- what the supplier receives -----------------------------------------------------------------------------------
def test_supplier_email_has_readable_attachment_names(tmp_path):
    pdf = tmp_path / "ab12cd34_Structural Drawings.pdf"
    pdf.write_bytes(documents.sanitise_pdf(make_pdf()))
    img1, img2 = tmp_path / "9f8e7d6c5b4a.jpg", tmp_path / "1a2b3c4d5e6f.jpg"
    img1.write_bytes(jpeg_bytes()); img2.write_bytes(jpeg_bytes())

    msg = build_mime_message("supplier@example.com", "RFQ", "body", [str(img1), str(pdf), str(img2)])
    parsed = email.message_from_bytes(msg.as_bytes())
    names = [(p.get_content_type(), p.get_filename()) for p in parsed.walk() if p.get_filename()]
    assert names == [("image/jpeg", "photo-1.jpg"), ("application/pdf", "Structural Drawings.pdf"), ("image/jpeg", "photo-2.jpg")]


def test_display_name_rules():
    assert display_name(Path("deadbeef_Plans Rev C.pdf"), 1) == "Plans Rev C.pdf"
    assert display_name(Path("Plans.pdf"), 1) == "Plans.pdf"  # no storage prefix: left alone
    assert display_name(Path("abcdef123456.jpg"), 3) == "photo-3.jpg"


def test_supplier_file_names_number_the_photos_and_keep_document_names_in_send_order():
    from services.attachments import supplier_file_names

    paths = ["/x/abcdef123456.jpg", "/x/deadbeef_Plans Rev C.pdf", "/x/0123456789ab.jpg"]
    assert supplier_file_names(paths) == ["photo-1.jpg", "Plans Rev C.pdf", "photo-2.jpg"]
    assert supplier_file_names([]) == [] and supplier_file_names(None) == []
