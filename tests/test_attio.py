from unittest.mock import patch

from linkedin.notifications import attio


# ---------------------------------------------------------------------------
# A.1 — Outreach-status rank table completeness
# ---------------------------------------------------------------------------


def test_outreach_rank_includes_all_active_statuses():
    expected = {
        attio.STATUS_INVITE_SENT: 1,
        attio.STATUS_CONNECTED: 2,
        attio.STATUS_REPLIED: 3,
        attio.STATUS_WANTS_MEETING: 4,
        attio.STATUS_MEETING_BOOKED: 5,
        attio.STATUS_HAD_MEETING: 6,
        attio.STATUS_PROSPECTING_TO_CLOSE: 7,
        attio.STATUS_WON: 8,
    }
    assert attio.OUTREACH_RANK == expected


def test_should_patch_outreach_status_blocks_demotion_from_had_meeting():
    assert attio.should_patch_outreach_status(
        attio.STATUS_HAD_MEETING, attio.STATUS_REPLIED,
    ) is False


def test_should_patch_outreach_status_blocks_demotion_from_meeting_booked_to_wants_meeting():
    assert attio.should_patch_outreach_status(
        attio.STATUS_MEETING_BOOKED, attio.STATUS_WANTS_MEETING,
    ) is False


def test_should_patch_outreach_status_blocks_demotion_from_prospecting_to_close():
    assert attio.should_patch_outreach_status(
        attio.STATUS_PROSPECTING_TO_CLOSE, attio.STATUS_HAD_MEETING,
    ) is False


def test_should_patch_outreach_status_allows_promotion_to_wants_meeting():
    assert attio.should_patch_outreach_status(
        attio.STATUS_REPLIED, attio.STATUS_WANTS_MEETING,
    ) is True
    assert attio.should_patch_outreach_status(
        attio.STATUS_CONNECTED, attio.STATUS_WANTS_MEETING,
    ) is True


def test_should_patch_outreach_status_won_overrides_anything():
    assert attio.should_patch_outreach_status(
        attio.STATUS_HAD_MEETING, attio.STATUS_WON,
    ) is True


def test_should_patch_outreach_status_lost_is_overridable():
    assert attio.should_patch_outreach_status(
        attio.STATUS_LOST, attio.STATUS_REPLIED,
    ) is True


# ---------------------------------------------------------------------------
# A.2 — create_person uses PUT ?matching_attribute=linkedin
# ---------------------------------------------------------------------------


def _fake_attio_response(record_id: str = "rec_abc123") -> dict:
    return {"data": {"id": {"record_id": record_id}}}


@patch("linkedin.notifications.attio._request")
def test_create_person_posts_with_linkedin_url(mock_request):
    """create_person POSTs (always creates fresh).

    Assert-pattern dedupe was attempted but reverted: Attio requires the
    matching attribute (`linkedin`) to be marked unique in the workspace,
    which it isn't by default. If the user marks it unique later, switch
    create_person back to PUT ?matching_attribute=linkedin.
    """
    mock_request.return_value = _fake_attio_response("rec_xyz")

    pid = attio.create_person(
        first_name="Waylon",
        last_name="Krush",
        linkedin_url="https://www.linkedin.com/in/waylonkrush/",
    )

    assert pid == "rec_xyz"
    call = mock_request.call_args
    method, path = call.args[0], call.args[1]
    body = call.args[2]
    assert method == "POST"
    assert path == "/objects/people/records"
    assert body["data"]["values"]["linkedin"] == "https://www.linkedin.com/in/waylonkrush/"


@patch("linkedin.notifications.attio._request")
def test_create_person_works_without_linkedin_url(mock_request):
    mock_request.return_value = _fake_attio_response("rec_no_li")

    pid = attio.create_person(first_name="Jane", last_name="Doe")

    assert pid == "rec_no_li"
    call = mock_request.call_args
    method, path = call.args[0], call.args[1]
    assert method == "POST"
    assert path == "/objects/people/records"


# ---------------------------------------------------------------------------
# A.3 — create_company uses PUT ?matching_attribute=name
# ---------------------------------------------------------------------------


@patch("linkedin.notifications.attio._request")
def test_create_company_posts(mock_request):
    """create_company POSTs (always creates fresh).

    Assert-pattern reverted: Attio requires `name` to be marked unique for
    PUT ?matching_attribute=name to work, but `name` legitimately isn't
    unique (multiple companies can share a name). If you start populating
    `domains` and mark that unique, switch to PUT ?matching_attribute=domains.
    """
    mock_request.return_value = _fake_attio_response("rec_co_1")

    cid = attio.create_company("Acme Inc")

    assert cid == "rec_co_1"
    call = mock_request.call_args
    method, path = call.args[0], call.args[1]
    body = call.args[2]
    assert method == "POST"
    assert path == "/objects/companies/records"
    assert body["data"]["values"]["name"] == "Acme Inc"


# ---------------------------------------------------------------------------
# D.3 — add_person_email helper
# ---------------------------------------------------------------------------


@patch("linkedin.notifications.attio._request")
def test_add_person_email_appends_when_existing_emails_present(mock_request):
    mock_request.side_effect = [
        {"data": {"values": {"email_addresses": [
            {"email_address": "existing@old.com"},
        ]}}},
        {"data": {"id": {"record_id": "rec_p1"}}},
    ]
    attio.add_person_email("rec_p1", "new@example.com")
    patch_call = mock_request.call_args_list[1]
    method, path = patch_call.args[0], patch_call.args[1]
    body = patch_call.args[2]
    emails = [e["email_address"] for e in body["data"]["values"]["email_addresses"]]
    assert method == "PATCH"
    assert path == "/objects/people/records/rec_p1"
    assert "existing@old.com" in emails
    assert "new@example.com" in emails


@patch("linkedin.notifications.attio._request")
def test_add_person_email_no_op_when_already_present(mock_request):
    mock_request.return_value = {"data": {"values": {"email_addresses": [
        {"email_address": "x@y.com"},
    ]}}}
    attio.add_person_email("rec_p1", "x@y.com")
    assert mock_request.call_count == 1


@patch("linkedin.notifications.attio._request")
def test_add_person_email_skips_when_email_blank(mock_request):
    attio.add_person_email("rec_p1", "")
    assert mock_request.call_count == 0
