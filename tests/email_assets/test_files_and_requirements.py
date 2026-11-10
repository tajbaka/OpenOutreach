from email import message_from_bytes, policy
from email.message import EmailMessage

import pytest

from email_assets.files import (
    MAX_EMAIL_ATTACHMENT_BYTES,
    AttachmentFile,
    AttachmentFileError,
    add_attachments_to_message,
    clean_filename,
    validate_attachment_set,
)
from email_assets.requirements import (
    AttachmentRequirement,
    AttachmentRequirementError,
    parse_attachment_requirements,
)
from tests.email_assets.helpers import pdf_bytes, sha


def _pdf(name="acme.pdf", label="acme"):
    content = pdf_bytes(label)
    return AttachmentFile(filename=name, mime_type="application/pdf", content=content, sha256=sha(content))


def test_attachment_file_proves_its_bytes_match_their_hash():
    content = pdf_bytes("one")
    with pytest.raises(AttachmentFileError, match="do not match"):
        AttachmentFile(
            filename="one.pdf",
            mime_type="application/pdf",
            content=content,
            sha256=sha(pdf_bytes("two")),
        )


def test_attachment_file_rejects_wrong_signature_type_and_unsafe_names():
    with pytest.raises(AttachmentFileError, match="not a valid application/pdf"):
        AttachmentFile(filename="x.pdf", mime_type="application/pdf", content=b"MZ\x90", sha256=sha(b"MZ\x90"))
    with pytest.raises(AttachmentFileError, match="not an allowed"):
        clean_filename("tool.exe", mime_type="application/x-msdownload")
    with pytest.raises(AttachmentFileError, match="path or control"):
        clean_filename("../acme.pdf", mime_type="application/pdf")
    with pytest.raises(AttachmentFileError, match="must end with"):
        clean_filename("acme.docx", mime_type="application/pdf")
    assert clean_filename("  Acme   Marketplace.PDF ", mime_type="application/pdf") == "Acme Marketplace.PDF"


def test_attachment_set_limits_count_size_and_duplicate_names():
    with pytest.raises(AttachmentFileError, match="one filename"):
        validate_attachment_set([_pdf("a.pdf", "1"), _pdf("A.pdf", "2")])
    big = b"%PDF-" + b"0" * (MAX_EMAIL_ATTACHMENT_BYTES // 2)
    files = [
        AttachmentFile(filename=f"{n}.pdf", mime_type="application/pdf", content=big + bytes([n]), sha256=sha(big + bytes([n])))
        for n in range(3)
    ]
    with pytest.raises(AttachmentFileError, match="total"):
        validate_attachment_set(files)


def test_mime_assembly_keeps_body_and_attaches_exact_bytes():
    message = EmailMessage()
    message["To"] = "ada@example.com"
    message.set_content("Hi Ada,\nThe PDF is attached.")
    attachment = _pdf()
    add_attachments_to_message(message, [attachment])

    parsed = message_from_bytes(message.as_bytes(), policy=policy.default)
    assert parsed.get_content_type() == "multipart/mixed"
    assert parsed.get_body(preferencelist=("plain",)).get_content() == "Hi Ada,\nThe PDF is attached.\n"
    [part] = list(parsed.iter_attachments())
    assert part.get_filename() == "acme.pdf"
    assert part.get_content_type() == "application/pdf"
    assert sha(part.get_payload(decode=True)) == attachment.sha256


def test_requirements_parse_slots_fixed_assets_and_optional_flag():
    parsed = parse_attachment_requirements([
        {"slot": "marketplace_pdf"},
        {"asset": "boundera-overview", "required": False},
    ])
    assert parsed == (
        AttachmentRequirement(slot="marketplace_pdf"),
        AttachmentRequirement(asset="boundera-overview", required=False),
    )
    assert parsed[0].payload() == {"slot": "marketplace_pdf", "required": True}


@pytest.mark.parametrize("value, message", [
    ("marketplace_pdf", "must be a list"),
    ([{"slot": "a", "asset": "b"}], "exactly one"),
    ([{}], "exactly one"),
    ([{"slot": "Bad Slot"}], "lowercase key"),
    ([{"slot": "a", "file": "x"}], "unknown fields"),
    ([{"slot": "a", "required": "yes"}], "true or false"),
    ([{"slot": "a"}, {"slot": "a"}], "repeats"),
])
def test_requirements_reject_malformed_declarations(value, message):
    with pytest.raises(AttachmentRequirementError, match=message):
        parse_attachment_requirements(value)
