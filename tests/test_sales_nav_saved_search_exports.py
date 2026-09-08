import csv
import json
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from linkedin.actions.sales_nav_export import (
    CSV_HEADER,
    SalesNavExportStats,
    export_sales_nav_csv,
)
from linkedin.actions.sales_nav_list import (
    discover_search_url_template,
    iter_sales_nav_list,
)
from linkedin.actions.sales_nav_saved_searches import (
    SavedSalesSearch,
    discover_saved_people_searches,
    parse_saved_people_search_links,
    validate_people_search_url,
)
from linkedin.exceptions import SalesNavigatorSurfaceError
from linkedin.management.commands.export_sales_saved_searches import (
    Command as BatchExportCommand,
)


def test_parse_saved_people_search_links_scopes_and_deduplicates():
    links = [
        {
            "label": "View FMKT | Enterprise | GRC Staff lead saved search",
            "href": "/sales/search/people?savedSearchId=101",
        },
        {
            "label": "View unrelated search lead saved search",
            "href": "/sales/search/people?savedSearchId=102",
        },
        {
            "label": "View FMKT | Duplicate lead saved search",
            "href": "/sales/search/people?savedSearchId=101",
        },
    ]

    searches = parse_saved_people_search_links(links, name_prefix="FMKT |")

    assert searches == [
        SavedSalesSearch(
            name="FMKT | Enterprise | GRC Staff",
            saved_search_id="101",
            url="https://www.linkedin.com/sales/search/people?savedSearchId=101",
        )
    ]


def test_parse_saved_people_search_links_can_require_suffix():
    links = [
        {
            "label": "View FMKT | Enterprise | GRC Staff - NEW lead saved search",
            "href": "/sales/search/people?savedSearchId=103",
        },
        {
            "label": "View FMKT | Enterprise | GRC Staff lead saved search",
            "href": "/sales/search/people?savedSearchId=104",
        },
    ]

    searches = parse_saved_people_search_links(
        links,
        name_prefix="FMKT |",
        name_suffix=" - NEW",
    )

    assert [search.saved_search_id for search in searches] == ["103"]


@pytest.mark.parametrize(
    "url",
    [
        "http://www.linkedin.com/sales/search/people",
        "https://evil.example/sales/search/people",
        "https://www.linkedin.com/sales/search/accounts",
    ],
)
def test_validate_people_search_url_rejects_wrong_surface(url):
    with pytest.raises(SalesNavigatorSurfaceError):
        validate_people_search_url(url)


def test_parse_saved_people_search_links_fails_closed_on_drift():
    with pytest.raises(SalesNavigatorSurfaceError, match="Unexpected"):
        parse_saved_people_search_links(
            [{"label": "Open search", "href": "/sales/search/people?id=1"}],
            name_prefix="FMKT |",
        )


def test_discover_saved_people_searches_uses_exact_read_only_controls(mocker):
    trigger = mocker.Mock()
    first = mocker.Mock()
    anchors = mocker.Mock()
    anchors.first = first
    anchors.count.side_effect = [2, 2, 2]
    anchors.evaluate_all.return_value = [
        {
            "label": "View FMKT | Small CSP | Founder lead saved search",
            "href": "/sales/search/people?savedSearchId=201",
        }
    ]
    dialog = mocker.Mock()
    dialog.locator.return_value = anchors
    dialog_filter = mocker.Mock(return_value=dialog)
    dialog_root = mocker.Mock(filter=dialog_filter)
    page = mocker.Mock()
    page.url = "https://www.linkedin.com/sales/search/people"
    page.locator.return_value = trigger
    page.get_by_role.return_value = dialog_root
    session = SimpleNamespace(page=page)

    searches = discover_saved_people_searches(session, name_prefix="FMKT |")

    assert searches[0].saved_search_id == "201"
    page.locator.assert_called_once_with(
        "button[data-x--link--saved-searches]"
    )
    trigger.click.assert_called_once_with()
    dialog.locator.assert_called_once_with(
        "a[data-x--saved-search-panel--saved-search-link]"
    )


def test_export_sales_nav_csv_writes_complete_file_and_counts(monkeypatch, tmp_path):
    rows = [
        {
            "member_urn": "one",
            "first_name": "A",
            "last_name": "One",
            "full_name": "A One",
            "company_name": "Alpha",
            "title": "GRC Manager",
            "geo_region": "US",
            "degree": "2nd",
        },
        {
            "member_urn": "two",
            "first_name": "B",
            "last_name": "Two",
            "full_name": "B Two",
            "company_name": "Beta",
            "title": "Compliance Analyst",
            "geo_region": "US",
            "degree": None,
        },
        {
            "member_urn": "three",
            "first_name": "C",
            "last_name": "Three",
            "full_name": "C Three",
            "company_name": "Gamma",
            "title": "Security Assurance",
            "geo_region": "US",
            "degree": None,
        },
    ]
    monkeypatch.setattr(
        "linkedin.actions.sales_nav_export.iter_sales_nav_list",
        lambda *_args, **_kwargs: iter(rows),
    )

    class FakeAPI:
        def get_profile(self, public_identifier):
            if public_identifier == "one":
                return (
                    {
                        "public_identifier": "a-one",
                        "url": "https://www.linkedin.com/in/a-one/",
                        "first_name": "A",
                        "last_name": "One",
                    },
                    {},
                )
            if public_identifier == "two":
                return None, None
            return {}, {}

    output = tmp_path / "search.csv"
    stats = export_sales_nav_csv(
        FakeAPI(),
        url_template="https://example.test?start={start}&count={count}",
        output_path=output,
        delay_seconds=0,
    )

    assert stats == SalesNavExportStats(
        seen=3, written=1, inaccessible=1, unresolvable=1
    )
    assert not (tmp_path / "search.partial.csv").exists()
    with output.open(newline="", encoding="utf-8") as handle:
        exported = list(csv.reader(handle))
    assert exported[0] == CSV_HEADER
    assert exported[1] == [
        "https://www.linkedin.com/in/a-one/",
        "A",
        "One",
        "Alpha",
        "GRC Manager",
        "US",
        "2nd",
    ]


def test_export_sales_nav_csv_preserves_partial_and_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "linkedin.actions.sales_nav_export.iter_sales_nav_list",
        lambda *_args, **_kwargs: iter([
            {
                "member_urn": "one",
                "first_name": "A",
                "last_name": "One",
                "full_name": "A One",
                "company_name": "Alpha",
                "title": "Security",
                "geo_region": "US",
                "degree": None,
            }
        ]),
    )

    class BrokenAPI:
        def get_profile(self, public_identifier):
            raise RuntimeError("unexpected")

    output = tmp_path / "broken.csv"
    with pytest.raises(RuntimeError, match="unexpected"):
        export_sales_nav_csv(
            BrokenAPI(),
            url_template="https://example.test?start={start}&count={count}",
            output_path=output,
            delay_seconds=0,
        )

    assert not output.exists()
    assert (tmp_path / "broken.partial.csv").exists()


def test_iter_sales_nav_list_rejects_promised_but_empty_page():
    response = SimpleNamespace(
        ok=True,
        status=200,
        json=lambda: {"elements": [], "paging": {"total": 2}},
        text=lambda: "{}",
    )
    api = SimpleNamespace(get=lambda *_args, **_kwargs: response)

    with pytest.raises(SalesNavigatorSurfaceError, match="promised 2 results"):
        list(
            iter_sales_nav_list(
                api,
                "",
                url_template="https://example.test?start={start}&count={count}",
            )
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"elements": [], "paging": {}},
        {"elements": {}, "paging": {"total": 0}},
    ],
)
def test_iter_sales_nav_list_rejects_ambiguous_empty_payload(payload):
    response = SimpleNamespace(
        ok=True,
        status=200,
        json=lambda: payload,
        text=lambda: "{}",
    )
    api = SimpleNamespace(get=lambda *_args, **_kwargs: response)

    with pytest.raises(SalesNavigatorSurfaceError, match="malformed|paging total"):
        list(
            iter_sales_nav_list(
                api,
                "",
                url_template="https://example.test?start={start}&count={count}",
            )
        )


def test_discover_search_url_template_skips_unfiltered_saved_search_prefetch():
    callbacks = []

    class FakeResponse:
        status = 200

        def __init__(self, saved_search_id, *, query=None, payload=None):
            query_param = f"&query={query}" if query is not None else ""
            self.url = (
                "https://www.linkedin.com/sales-api/salesApiLeadSearch"
                f"?savedSearchId={saved_search_id}&start=0&count=25"
                f"{query_param}"
            )
            self.payload = payload or {
                "elements": [{}],
                "paging": {"total": 1},
            }

        def json(self):
            return self.payload

    class FakePage:
        @staticmethod
        def on(event, callback):
            assert event == "response"
            callbacks.append(callback)

        @staticmethod
        def goto(_url):
            callbacks[0](
                FakeResponse("1234", query="%28filters%3AList%28%29%29")
            )
            callbacks[0](FakeResponse("123"))
            callbacks[0](
                FakeResponse(
                    "123",
                    query="invalid-filter-request",
                    payload={"elements": {}, "paging": {"total": 1}},
                )
            )
            callbacks[0](
                FakeResponse(
                    "123",
                    query="%28filters%3AList%28%28type%3ACURRENT_TITLE%29%29%29",
                )
            )

        @staticmethod
        def wait_for_load_state(_state):
            return None

        @staticmethod
        def wait_for_timeout(_milliseconds):
            return None

        @staticmethod
        def remove_listener(_event, _callback):
            return None

    template = discover_search_url_template(
        SimpleNamespace(page=FakePage()),
        "https://www.linkedin.com/sales/search/people?savedSearchId=123",
    )

    assert "savedSearchId=123&" in template
    assert "savedSearchId=1234" not in template
    assert "query=%28filters%3AList%28%28type%3ACURRENT_TITLE%29%29%29" in template


def test_discover_search_url_template_preserves_ordinary_search_capture():
    callbacks = []

    class FakeResponse:
        status = 200
        url = (
            "https://www.linkedin.com/sales-api/salesApiPeopleSearch"
            "?q=peopleSearchQuery&start=0&count=25"
        )

        @staticmethod
        def json():
            return {"elements": [], "paging": {"total": 0}}

    class FakePage:
        @staticmethod
        def on(event, callback):
            assert event == "response"
            callbacks.append(callback)

        @staticmethod
        def goto(_url):
            callbacks[0](FakeResponse())

        @staticmethod
        def wait_for_load_state(_state):
            return None

        @staticmethod
        def wait_for_timeout(_milliseconds):
            return None

        @staticmethod
        def remove_listener(_event, _callback):
            return None

    template = discover_search_url_template(
        SimpleNamespace(page=FakePage()),
        "https://www.linkedin.com/sales/search/people?query=%28filters%3AList%28%29%29",
    )

    assert template.endswith("start={start}&count={count}")


def test_iter_sales_nav_list_rejects_missing_pagination_placeholders():
    with pytest.raises(SalesNavigatorSurfaceError, match="both"):
        list(
            iter_sales_nav_list(
                SimpleNamespace(),
                "",
                url_template="https://example.test?start={start}",
            )
        )


def test_iter_sales_nav_list_rejects_result_without_member_urn():
    response = SimpleNamespace(
        ok=True,
        status=200,
        json=lambda: {
            "elements": [{"fullName": "Changed Shape"}],
            "paging": {"total": 1},
        },
        text=lambda: "{}",
    )
    api = SimpleNamespace(get=lambda *_args, **_kwargs: response)

    with pytest.raises(SalesNavigatorSurfaceError, match="member URN"):
        list(
            iter_sales_nav_list(
                api,
                "",
                url_template="https://example.test?start={start}&count={count}",
            )
        )


def test_resume_manifest_rejects_limited_probe_for_unlimited_run(tmp_path):
    command = BatchExportCommand()
    search = SavedSalesSearch(
        "FMKT | Enterprise | GRC Staff",
        "401",
        "https://www.linkedin.com/sales/search/people?savedSearchId=401",
    )
    output_dir = tmp_path / "batch"
    output_dir.mkdir()
    manifest_path = output_dir / "manifest.json"
    manifest = command._new_manifest(
        "sales@example.com",
        "https://www.linkedin.com/sales/search/people",
        "FMKT |",
        None,
        output_dir,
        1,
        [search],
    )
    manifest["searches"][0].update(status="limited_complete", seen=1, written=1)
    command._write_manifest(manifest_path, manifest)

    compatible = command._load_resume_manifest(
        manifest_path,
        username="sales@example.com",
        bootstrap_url="https://www.linkedin.com/sales/search/people",
        name_prefix="FMKT |",
        name_suffix=None,
        output_dir=output_dir,
        limit_per_search=1,
        searches=[search],
    )
    assert compatible["searches"][0]["status"] == "limited_complete"

    with pytest.raises(CommandError, match="limit_per_search"):
        command._load_resume_manifest(
            manifest_path,
            username="sales@example.com",
            bootstrap_url="https://www.linkedin.com/sales/search/people",
            name_prefix="FMKT |",
            name_suffix=None,
            output_dir=output_dir,
            limit_per_search=None,
            searches=[search],
        )


def test_batch_command_reuses_one_session_and_writes_manifest(
    monkeypatch, tmp_path
):
    searches = [
        SavedSalesSearch(
            "FMKT | Enterprise | GRC Staff",
            "301",
            "https://www.linkedin.com/sales/search/people?savedSearchId=301",
        ),
        SavedSalesSearch(
            "FMKT | Mid-Market | GRC Staff",
            "302",
            "https://www.linkedin.com/sales/search/people?savedSearchId=302",
        ),
    ]
    session_state = {"entered": 0, "exited": 0}

    class FakeSession:
        username = "sales@example.com"

        def __init__(self, **_kwargs):
            self.page = object()

        def __enter__(self):
            session_state["entered"] += 1
            return self

        def __exit__(self, *_args):
            session_state["exited"] += 1

    monkeypatch.setattr(
        "linkedin.actions.standalone_session.StandaloneLinkedInSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "linkedin.actions.sales_nav_saved_searches.discover_saved_people_searches",
        lambda *_args, **_kwargs: searches,
    )
    monkeypatch.setattr(
        "linkedin.actions.sales_nav_list.discover_search_url_template",
        lambda _session, url: (
            "https://www.linkedin.com/sales-api/salesApiLeadSearch"
            f"?savedSearchId={url.rsplit('=', 1)[-1]}&start={{start}}&count={{count}}"
        ),
    )
    monkeypatch.setattr(
        "linkedin.api.client.PlaywrightLinkedinAPI",
        lambda session: SimpleNamespace(session=session),
    )

    def fake_export(_api, *, output_path, **_kwargs):
        output_path.write_text(
            ",".join(CSV_HEADER) + "\nhttps://linkedin.test/a,A,One,Alpha,GRC,US,2nd\n",
            encoding="utf-8",
        )
        return SalesNavExportStats(1, 1, 0, 0)

    monkeypatch.setattr(
        "linkedin.actions.sales_nav_export.export_sales_nav_csv",
        fake_export,
    )

    output_dir = tmp_path / "batch"
    stdout = StringIO()
    call_command(
        "export_sales_saved_searches",
        output_dir=str(output_dir),
        delay_seconds=0,
        stdout=stdout,
    )

    assert session_state == {"entered": 1, "exited": 1}
    manifest = json.loads((output_dir / "manifest.json").read_text())
    assert [row["status"] for row in manifest["searches"]] == [
        "complete",
        "complete",
    ]
    assert [row["saved_search_id"] for row in manifest["searches"]] == [
        "301",
        "302",
    ]
    assert all(
        (output_dir / row["output_file"]).exists()
        for row in manifest["searches"]
    )
    assert (output_dir / "manifest.json").stat().st_mode & 0o777 == 0o600


def test_batch_command_rejects_blank_prefix_before_browser(tmp_path):
    with pytest.raises(CommandError, match="name-prefix"):
        call_command(
            "export_sales_saved_searches",
            name_prefix=" ",
            output_dir=str(tmp_path / "batch"),
        )


def test_batch_command_rejects_blank_suffix_before_browser(tmp_path):
    with pytest.raises(CommandError, match="name-suffix"):
        call_command(
            "export_sales_saved_searches",
            name_suffix=" ",
            output_dir=str(tmp_path / "batch"),
        )
