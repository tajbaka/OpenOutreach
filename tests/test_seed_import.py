from linkedin.setup.seeds import parse_csv_leads


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
