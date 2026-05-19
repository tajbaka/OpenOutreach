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
