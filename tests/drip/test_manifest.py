from copy import deepcopy

import pytest

from drip.exceptions import ManifestValidationError
from drip.manifest import render_template, validate_manifest


def test_valid_manifest_normalizes_and_hashes_deterministically(valid_drip_payload):
    first = validate_manifest(valid_drip_payload)
    reordered = {
        "audiences": valid_drip_payload["audiences"],
        "name": valid_drip_payload["name"],
        "campaign_key": valid_drip_payload["campaign_key"],
        "schema_version": 3,
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


def test_gmail_structured_link_is_normalized_and_hashed(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    step = payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "gmail"
    ][0]
    step["body"] = "Hi {first_name}, see {tracked_link}"
    step["link"] = {
        "key": "fedramp_automation",
        "url": "https://BOUNDERA.io/fedramp-automation?view=gap%20report#details",
    }

    validated = validate_manifest(payload)
    normalized = validated.normalized["audiences"]["CSPs"]["themes"][0][
        "senders"
    ]["Arian"]["gmail"][0]

    assert normalized["body"] == "Hi {first_name}, see {tracked_link}"
    assert normalized["link"] == {
        "key": "fedramp_automation",
        "url": "https://boundera.io/fedramp-automation?view=gap+report#details",
    }
    assert validate_manifest(deepcopy(payload)).content_hash == validated.content_hash


@pytest.mark.parametrize(
    ("mutate", "message"),
    (
        (
            lambda step: step.update(
                {"link": {"key": "product", "url": "https://boundera.io/product"}},
            ),
            "exactly one.*tracked_link",
        ),
        (
            lambda step: step.update({"body": "See {tracked_link}"}),
            "without a configured link",
        ),
        (
            lambda step: step.update(
                {
                    "body": "See {tracked_link} twice {tracked_link}",
                    "link": {"key": "product", "url": "https://boundera.io/product"},
                },
            ),
            "exactly one.*tracked_link",
        ),
        (
            lambda step: step.update(
                {"body": "See https://boundera.io/product?ref=oo_literal"},
            ),
            "literal reserved",
        ),
        (
            lambda step: step.update(
                {
                    "body": "See {tracked_link}",
                    "link": {"key": "product", "url": "https://example.com/product"},
                },
            ),
            "allowed Boundera host",
        ),
    ),
)
def test_gmail_structured_link_rejects_unsafe_shapes(
    valid_drip_payload,
    mutate,
    message,
):
    payload = deepcopy(valid_drip_payload)
    step = payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "gmail"
    ][0]
    mutate(step)

    with pytest.raises(ManifestValidationError, match=message):
        validate_manifest(payload)


def test_link_object_and_tracked_placeholder_are_gmail_only(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    step = payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "linkedin"
    ][0]
    step["body"] = "See {tracked_link}"
    step["link"] = {"key": "product", "url": "https://boundera.io/product"}

    with pytest.raises(ManifestValidationError, match="unknown key.*link"):
        validate_manifest(payload)


def test_gmail_subject_cannot_contain_tracked_link(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    step = payload["audiences"]["CSPs"]["themes"][0]["senders"]["Arian"][
        "gmail"
    ][0]
    step["subject"] = "See {tracked_link}"

    with pytest.raises(ManifestValidationError, match="cannot use.*tracked_link"):
        validate_manifest(payload)


def test_previous_schema_is_not_republished_as_current(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    payload["schema_version"] = 1

    with pytest.raises(ManifestValidationError, match="must equal 3"):
        validate_manifest(payload)
