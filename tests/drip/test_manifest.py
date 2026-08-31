from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from drip.exceptions import ManifestValidationError
from drip.manifest import render_template, validate_manifest
from linkedin.message_media import resolve_linkedin_media


def test_valid_manifest_normalizes_and_hashes_deterministically(valid_drip_payload):
    first = validate_manifest(valid_drip_payload)
    reordered = {
        "audiences": valid_drip_payload["audiences"],
        "name": valid_drip_payload["name"],
        "campaign_key": valid_drip_payload["campaign_key"],
        "schema_version": 2,
    }
    second = validate_manifest(reordered)

    assert first.normalized == second.normalized
    assert first.content_hash == second.content_hash
    assert first.normalized["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "gmail"
    ][1] == {
        "delay_days": 3,
        "body": "Following up in the same thread.",
    }


def test_gmail_later_subject_may_be_omitted_or_repeat_first(valid_drip_payload):
    omitted = validate_manifest(valid_drip_payload)
    repeated_payload = deepcopy(valid_drip_payload)
    repeated_payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["gmail"][
        1
    ]["subject"] = "A question about {company_name}"
    repeated = validate_manifest(repeated_payload)

    assert repeated.content_hash == omitted.content_hash
    assert "subject" not in repeated.normalized["audiences"]["CSPs"]["themes"][0][
        "senders"
    ]["Arian"]["gmail"][1]


def test_gmail_later_subject_cannot_change_thread_subject(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["gmail"][1][
        "subject"
    ] = "A different subject"

    with pytest.raises(ManifestValidationError, match="exactly match the first"):
        validate_manifest(payload)


def test_gmail_first_step_requires_subject(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    del payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["gmail"][0][
        "subject"
    ]

    with pytest.raises(ManifestValidationError, match="missing required key.*subject"):
        validate_manifest(payload)


def test_gmail_subject_is_constant_across_themes_in_one_lane(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][1]["senders"]["Arian"]["gmail"][0][
        "subject"
    ] = "A new theme and a competing thread subject"

    with pytest.raises(ManifestValidationError, match="one lane uses one thread"):
        validate_manifest(payload)


def test_linkedin_media_is_resolved_and_frozen_in_normalized_manifest(
    valid_drip_payload,
):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["linkedin"][
        0
    ]["media"] = {"type": "gif", "file": "demo.gif"}

    validated = validate_manifest(payload)

    media = validated.normalized["audiences"]["CSPs"]["themes"][0]["senders"][
        "Arian"
    ]["linkedin"][0]["media"]
    asset_path = Path("assets/follow_up/demo.gif")
    assert media == {
        "type": "gif",
        "file": "demo.gif",
        "mime_type": "image/gif",
        "size_bytes": asset_path.stat().st_size,
        "sha256": hashlib.sha256(asset_path.read_bytes()).hexdigest(),
    }


def test_linkedin_video_media_is_accepted(valid_drip_payload, monkeypatch, tmp_path):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["linkedin"][
        0
    ]["media"] = {"type": "video", "file": "overview.mp4"}
    asset_root = tmp_path / "assets" / "follow_up"
    asset_root.mkdir(parents=True)
    asset_path = asset_root / "overview.mp4"
    asset_path.write_bytes((12).to_bytes(4, "big") + b"ftyp" + b"isom")

    def _resolver(reference, **kwargs):
        return resolve_linkedin_media(reference, root_dir=tmp_path, **kwargs)

    monkeypatch.setattr("drip.manifest.resolve_linkedin_media", _resolver)

    validated = validate_manifest(payload)

    media = validated.normalized["audiences"]["CSPs"]["themes"][0]["senders"][
        "Arian"
    ]["linkedin"][0]["media"]
    assert media["type"] == "video"
    assert media["mime_type"] == "video/mp4"
    assert media["size_bytes"] == 12


def test_manifest_hash_changes_when_media_bytes_change(
    valid_drip_payload,
    monkeypatch,
    tmp_path,
):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["linkedin"][
        0
    ]["media"] = {"type": "gif", "file": "changing.gif"}
    asset_root = tmp_path / "assets" / "follow_up"
    asset_root.mkdir(parents=True)
    asset_path = asset_root / "changing.gif"
    asset_path.write_bytes(b"GIF89a-alpha")

    def _resolver(reference, **kwargs):
        return resolve_linkedin_media(reference, root_dir=tmp_path, **kwargs)

    monkeypatch.setattr("drip.manifest.resolve_linkedin_media", _resolver)
    first = validate_manifest(payload)
    asset_path.write_bytes(b"GIF89a-bravo")
    second = validate_manifest(payload)

    assert first.content_hash != second.content_hash
    first_media = first.normalized["audiences"]["CSPs"]["themes"][0]["senders"][
        "Arian"
    ]["linkedin"][0]["media"]
    second_media = second.normalized["audiences"]["CSPs"]["themes"][0]["senders"][
        "Arian"
    ]["linkedin"][0]["media"]
    assert first_media["sha256"] != second_media["sha256"]
    assert first_media["size_bytes"] == second_media["size_bytes"]


def test_gmail_rejects_media(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["gmail"][0][
        "media"
    ] = {"type": "gif", "file": "demo.gif"}

    with pytest.raises(ManifestValidationError, match=r"unknown key\(s\): media"):
        validate_manifest(payload)


@pytest.mark.parametrize(
    ("media", "message"),
    [
        ({"type": "image", "file": "demo.gif"}, "either 'gif' or 'video'"),
        ({"type": "gif", "file": "missing.gif"}, "does not exist"),
        ({"type": "gif", "file": "demo.gif", "caption": "no"}, "unknown key"),
    ],
)
def test_linkedin_rejects_invalid_media(valid_drip_payload, media, message):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"]["linkedin"][
        0
    ]["media"] = media

    with pytest.raises(ManifestValidationError, match=message):
        validate_manifest(payload)


def test_manifest_rejects_obsolete_schema_version(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    payload["schema_version"] = 1

    with pytest.raises(ManifestValidationError, match="must equal 2"):
        validate_manifest(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["audiences"].update({"Unknown ICP": value["audiences"].pop("CSPs")}),
            "not a canonical ICP",
        ),
        (
            lambda value: value["audiences"]["CSPs"]["themes"][0]["senders"].update(
                {"Eddy": value["audiences"]["CSPs"]["themes"][0]["senders"].pop("Arian")},
            ),
            "non-canonical sender",
        ),
        (
            lambda value: value["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
                "linkedin"
            ][0].update({"body": "Hello {unknown}"}),
            "unsupported placeholder",
        ),
        (
            lambda value: value["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
                "linkedin"
            ][0].update({"delay_days": -1}),
            "finite nonnegative",
        ),
    ],
)
def test_manifest_rejects_noncanonical_or_unsafe_content(
    valid_drip_payload,
    mutation,
    message,
):
    payload = deepcopy(valid_drip_payload)
    mutation(payload)

    with pytest.raises(ManifestValidationError, match=message):
        validate_manifest(payload)


def test_sender_set_must_be_complete_across_themes(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    payload["audiences"]["CSPs"]["themes"][0]["senders"]["Athena"] = {
        "linkedin": [{"delay_days": 0, "body": "Athena rendition"}],
    }

    with pytest.raises(ManifestValidationError, match="same canonical sender set"):
        validate_manifest(payload)


def test_render_template_requires_complete_allowlisted_context():
    assert render_template(
        "Hi {first_name} at {company_name}",
        {"first_name": "Ada", "company_name": "Analytical Engines"},
    ) == "Hi Ada at Analytical Engines"

    with pytest.raises(ManifestValidationError, match="missing render value"):
        render_template("Hi {first_name}", {})
