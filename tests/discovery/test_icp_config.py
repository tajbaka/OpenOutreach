import json

import pytest

from linkedin import icp_outbound
from linkedin.exceptions import DiscoveryConfigurationError


def _write(tmp_path, payload):
    path = tmp_path / "icp_messages.json"
    path.write_text(json.dumps(payload))
    return path


def test_loads_only_enabled_discovery_icps(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        {
            "Arian": {
                "CSPs": {
                    "discovery": {
                        "enabled": True,
                        "profile": "Cloud security leaders",
                        "search_queries": ["FedRAMP CISO", "FedRAMP CISO"],
                    },
                    "linkedin_connect_note": ["hello"],
                },
                "Advisors": {
                    "discovery": {"enabled": False},
                    "linkedin_connect_note": ["hello"],
                },
                "Channel": {"linkedin_connect_note": ["hello"]},
            },
        },
    )
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    targets = icp_outbound.load_discovery_targets("Arian")

    assert len(targets) == 1
    assert targets[0].icp == "CSPs"
    assert targets[0].search_queries == ("FedRAMP CISO",)


@pytest.mark.parametrize(
    "discovery, message",
    [
        ({"enabled": "yes", "profile": "x", "search_queries": ["q"]}, "boolean"),
        ({"enabled": True, "profile": "", "search_queries": ["q"]}, "profile"),
        ({"enabled": True, "profile": "x", "search_queries": []}, "search_queries"),
        (
            {
                "enabled": True,
                "profile": "x",
                "search_queries": ["q"],
                "campaign": 1,
            },
            "unknown discovery keys",
        ),
    ],
)
def test_malformed_discovery_block_fails(
    tmp_path,
    monkeypatch,
    discovery,
    message,
):
    path = _write(tmp_path, {"Arian": {"CSPs": {"discovery": discovery}}})
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    with pytest.raises(DiscoveryConfigurationError, match=message):
        icp_outbound.load_discovery_targets("Arian")


def test_sheet_save_preserves_discovery_metadata(tmp_path, monkeypatch):
    discovery = {
        "enabled": True,
        "profile": "Cloud security leaders",
        "search_queries": ["FedRAMP CISO"],
    }
    path = _write(
        tmp_path,
        {
            "Arian": {
                "CSPs": {
                    "discovery": discovery,
                    "linkedin_connect_note": ["old"],
                },
            },
        },
    )
    monkeypatch.setattr(icp_outbound, "_MESSAGES_PATH", path)

    icp_outbound.save_icp_messages(
        "Arian",
        {"CSPs": {"linkedin_connect_note": ["new"]}},
    )

    saved = json.loads(path.read_text())
    assert saved["Arian"]["CSPs"]["discovery"] == discovery
    assert saved["Arian"]["CSPs"]["linkedin_connect_note"] == ["new"]
