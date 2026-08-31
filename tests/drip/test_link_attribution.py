import re

import pytest

from drip.exceptions import LinkAttributionError
from drip.link_attribution import (
    MAX_ATTRIBUTED_URL_LENGTH,
    MAX_DESTINATION_URL_LENGTH,
    build_attributed_url,
    canonical_destination_url,
    generate_reference,
    reserved_references_in_text,
    validate_reference,
)


VALID_REFERENCE = "oo_EjRWeJCrze8SNFZ4kKvN7w"
SHARED_INVALID_REFERENCES = (
    "oo_short",
    "OO_EjRWeJCrze8SNFZ4kKvN7w",
    "oo_EjRWeJCrze8SNFZ4kKvN7!",
    "oo_EjRWeJCrze8SNFZ4kKvN7x",
    f" {VALID_REFERENCE}",
)


def test_reference_is_fixed_url_safe_128_bit_shape():
    references = {generate_reference() for _index in range(100)}

    assert len(references) == 100
    assert all(len(reference) == 25 for reference in references)
    assert all(re.fullmatch(r"oo_[A-Za-z0-9_-]{21}[AQgw]", reference) for reference in references)
    assert validate_reference(VALID_REFERENCE) == VALID_REFERENCE


@pytest.mark.parametrize(
    "value",
    (
        "",
        *SHARED_INVALID_REFERENCES,
        "arian@example.com",
        123,
    ),
)
def test_reference_rejects_invalid_or_identity_bearing_values(value):
    with pytest.raises(LinkAttributionError):
        validate_reference(value)


def test_destination_builder_preserves_query_and_fragment_canonically():
    destination = canonical_destination_url(
        "https://BOUNDERA.io/fedramp-automation?view=gap%20report&view=proof#details",
    )

    assert destination == (
        "https://boundera.io/fedramp-automation?view=gap+report&view=proof#details"
    )
    assert build_attributed_url(destination, VALID_REFERENCE) == (
        "https://boundera.io/fedramp-automation?view=gap+report&view=proof"
        f"&ref={VALID_REFERENCE}#details"
    )


def test_destination_length_reserves_space_for_attribution_suffix():
    prefix = "https://boundera.io/"
    destination = prefix + ("a" * (MAX_DESTINATION_URL_LENGTH - len(prefix)))

    assert canonical_destination_url(destination) == destination
    assert len(build_attributed_url(destination, VALID_REFERENCE)) == (
        MAX_ATTRIBUTED_URL_LENGTH
    )
    with pytest.raises(LinkAttributionError, match="too long"):
        canonical_destination_url(destination + "a")


@pytest.mark.parametrize(
    "url",
    (
        "http://boundera.io/path",
        "https://example.com/path",
        "https://user@boundera.io/path",
        "https://boundera.io:443/path",
        "https://boundera.io/path?ref=other",
        "https://boundera.io\\@example.com/path",
        "https://boundera.io/path?bad=%zz",
    ),
)
def test_destination_rejects_non_first_party_or_ambiguous_urls(url):
    with pytest.raises(LinkAttributionError):
        canonical_destination_url(url)


def test_reserved_reference_scan_returns_all_literal_body_references():
    other = "oo_0000000000000000000000"
    assert reserved_references_in_text(
        f"One https://boundera.io/a?ref={VALID_REFERENCE} and ref={other}",
    ) == (VALID_REFERENCE, other)
