"""Tests for the listener heartbeat file (pure file-I/O)."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from linkedin.realtime import heartbeat


def test_path_is_per_username(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    p1 = heartbeat.heartbeat_path_for("arian@tryfedrampgpt.com")
    p2 = heartbeat.heartbeat_path_for("chukyjack@gmail.com")
    assert p1 != p2
    assert p1.name == "listener-heartbeat-arian-tryfedrampgpt-com.json"


def test_empty_username_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    import pytest
    with pytest.raises(ValueError):
        heartbeat.heartbeat_path_for("")


def test_write_then_read_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    heartbeat.write_heartbeat("arian@x.com")
    last = heartbeat.read_heartbeat("arian@x.com")
    assert last is not None
    assert abs((timezone.now() - last).total_seconds()) < 5


def test_read_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    assert heartbeat.read_heartbeat("nobody@x.com") is None


def test_read_corrupt_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(heartbeat, "ROOT_DIR", tmp_path)
    path = heartbeat.heartbeat_path_for("arian@x.com")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert heartbeat.read_heartbeat("arian@x.com") is None
