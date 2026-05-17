"""Tests for cookie_store path helpers."""
from __future__ import annotations

import pytest

from linkedin.browser import cookie_store


def test_profile_dir_is_per_username(tmp_path, monkeypatch):
    monkeypatch.setattr(cookie_store, "ROOT_DIR", tmp_path)
    p1 = cookie_store.profile_dir_for("arian@tryfedrampgpt.com")
    p2 = cookie_store.profile_dir_for("chukyjack@gmail.com")
    assert p1 != p2
    assert p1.name == "profile-arian-tryfedrampgpt-com"
    assert p1.parent == tmp_path / "data"


def test_profile_dir_empty_username_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(cookie_store, "ROOT_DIR", tmp_path)
    with pytest.raises(ValueError):
        cookie_store.profile_dir_for("")
