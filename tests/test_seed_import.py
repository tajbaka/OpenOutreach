from types import SimpleNamespace

import pytest

from linkedin.exceptions import GeneralICPMessageError, SeedImportError
from linkedin.setup.seeds import parse_csv_leads


def test_parse_csv_preserves_exact_shared_keys_and_loads_once(monkeypatch):
    key = "reviewed-audience-" + "a" * 100
    calls = []

    def load():
        calls.append(True)
        return (SimpleNamespace(messages=(SimpleNamespace(audience_key=key),)),)

    monkeypatch.setattr("linkedin.general_icp_json.load_general_message_programs", load)
    rows = parse_csv_leads(
        "Profile URL,ICP\n"
        f"https://www.linkedin.com/in/qa-one/, {key} \n"
        f"https://www.linkedin.com/in/qa-two/,{key}\n"
        "https://www.linkedin.com/in/qa-three/,\n"
    )
    assert [row["icp"] for row in rows] == [key, key, ""]
    assert calls == [True]


@pytest.mark.parametrize("selection", ["typo-audience", "KNOWN-AUDIENCE"])
def test_parse_csv_rejects_unknown_or_case_changed_audience(monkeypatch, selection):
    monkeypatch.setattr("linkedin.general_icp_json.load_general_message_programs", lambda: (
        SimpleNamespace(messages=(SimpleNamespace(audience_key="known-audience"),)),
    ))
    with pytest.raises(SeedImportError, match="Unknown CSV ICP"):
        parse_csv_leads(
            "Profile URL,ICP\n"
            f"https://www.linkedin.com/in/qa-one/,{selection}\n"
        )


def test_parse_csv_does_not_mask_invalid_imported_programs(monkeypatch):
    def invalid():
        raise GeneralICPMessageError("mismatched shared programs")

    monkeypatch.setattr("linkedin.general_icp_json.load_general_message_programs", invalid)
    with pytest.raises(GeneralICPMessageError, match="mismatched"):
        parse_csv_leads("Profile URL,ICP\nhttps://www.linkedin.com/in/qa-one/,known-audience\n")


def test_parse_csv_leads_accepts_profile_url_header():
    rows = parse_csv_leads(
        "Profile URL,First Name\n"
        "https://www.linkedin.com/in/jane-doe/,Jane\n"
    )
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.linkedin.com/in/jane-doe/"
    assert rows[0]["first_name"] == "Jane"


def test_parse_csv_leads_accepts_linkedin_url_header():
    rows = parse_csv_leads(
        "LinkedIn URL,First Name\n"
        "https://www.linkedin.com/in/jane-doe/,Jane\n"
    )
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.linkedin.com/in/jane-doe/"
    assert rows[0]["first_name"] == "Jane"


def test_parse_csv_leads_normalizes_cmmc_icp_labels():
    rows = parse_csv_leads(
        "Profile URL,First Name,ICP\n"
        "https://www.linkedin.com/in/jane-doe/,Jane,CMMC Buyers\n"
        "https://www.linkedin.com/in/john-doe/,John,keep_advisor_channel\n"
    )
    assert [row["icp"] for row in rows] == [
        "CMMC Buyers",
        "CMMC Advisor/Channel",
    ]


def test_parse_csv_leads_normalizes_white_label_icp_labels():
    labels = [
        "White Label Product/Executive",
        "White Label Partnerships",
        "White Label Delivery",
        "White Label Champions",
    ]
    rows = parse_csv_leads(
        "Profile URL,First Name,ICP\n"
        + "".join(
            f"https://www.linkedin.com/in/lead-{idx}/,Lead,{label}\n"
            for idx, label in enumerate(labels)
        )
    )

    assert [row["icp"] for row in rows] == labels


def test_parse_csv_leads_normalizes_investor_channel_icp_labels():
    labels = [
        ("Investor / Portfolio Ops", "Investor / Portfolio Ops"),
        ("investor portfolio ops", "Investor / Portfolio Ops"),
        ("Accelerator / Ecosystem", "Accelerator / Ecosystem"),
        ("accelerator ecosystem", "Accelerator / Ecosystem"),
    ]
    rows = parse_csv_leads(
        "Profile URL,First Name,ICP\n"
        + "".join(
            f'https://www.linkedin.com/in/investor-{idx}/,Lead,"{label}"\n'
            for idx, (label, _expected) in enumerate(labels)
        )
    )

    assert [row["icp"] for row in rows] == [
        expected for _label, expected in labels
    ]


def test_parse_csv_leads_normalizes_stage_specific_csp_icp_labels():
    labels = [
        ("20x Initial Implementation", "20x Initial Implementation"),
        ("legacy ready", "Rev5 Ready"),
        ("Agency In Process", "Active FedRAMP Path"),
        ("FedRAMP In Process", "Active FedRAMP Path"),
        ("FedRAMP Certified or mature", "FedRAMP Mature"),
        (
            "Established federal portfolio, exact path verify",
            "CSP Stage Verify",
        ),
    ]
    rows = parse_csv_leads(
        "Profile URL,First Name,ICP\n"
        + "".join(
            f'https://www.linkedin.com/in/stage-{idx}/,Lead,"{label}"\n'
            for idx, (label, _expected) in enumerate(labels)
        )
    )

    assert [row["icp"] for row in rows] == [
        expected for _label, expected in labels
    ]
