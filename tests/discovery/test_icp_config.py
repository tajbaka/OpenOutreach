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
    assert targets[0].profile == "Cloud security leaders"


@pytest.mark.parametrize(
    "discovery, message",
    [
        ({"enabled": "yes", "profile": "x"}, "boolean"),
        ({"enabled": True, "profile": ""}, "profile"),
        (
            {
                "enabled": True,
                "profile": "x",
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


def test_checked_in_json_enables_all_non_cmmc_icps_for_every_sender():
    payload = json.loads(icp_outbound._MESSAGES_PATH.read_text())

    for sender, blocks in payload.items():
        for icp, channels in blocks.items():
            discovery = channels.get("discovery")
            if icp.startswith("CMMC"):
                assert discovery is None, f"{sender}/{icp} must not use discovery"
                continue
            assert discovery and discovery["enabled"] is True, (
                f"{sender}/{icp} must enable discovery"
            )
            assert discovery["profile"].strip()
            assert set(discovery) == {"enabled", "profile"}
