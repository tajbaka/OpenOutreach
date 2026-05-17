# tests/test_conf.py
import pytest
from unittest.mock import patch

from linkedin.conf import get_first_active_profile_handle


@pytest.mark.django_db
class TestGetFirstActiveProfileHandle:
    def test_returns_handle_when_profile_exists(self, fake_session):
        result = get_first_active_profile_handle()
        assert result == "testuser"

    def test_returns_none_when_no_profiles(self, db):
        from linkedin.models import LinkedInProfile
        LinkedInProfile.objects.all().delete()
        assert get_first_active_profile_handle() is None

    def test_returns_none_when_all_inactive(self, fake_session):
        from linkedin.models import LinkedInProfile
        LinkedInProfile.objects.all().update(active=False)
        assert get_first_active_profile_handle() is None


class TestRealtimeListenerConfig:
    def test_flag_defaults_off(self, monkeypatch):
        monkeypatch.delenv("ENABLE_REALTIME_LISTENER", raising=False)
        import importlib
        import linkedin.conf as conf
        importlib.reload(conf)
        assert conf.ENABLE_REALTIME_LISTENER is False
        # Restore module state for other tests.
        importlib.reload(conf)

    def test_flag_truthy_strings_enable(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        for raw in ("1", "true", "YES", "on"):
            monkeypatch.setenv("ENABLE_REALTIME_LISTENER", raw)
            importlib.reload(conf)
            assert conf.ENABLE_REALTIME_LISTENER is True
        importlib.reload(conf)

    def test_tuning_constants_have_defaults(self, monkeypatch):
        import importlib
        import linkedin.conf as conf
        monkeypatch.delenv("LISTENER_CATCHUP_GAP_MINUTES", raising=False)
        monkeypatch.delenv("LISTENER_PUMP_SLICE_SECONDS", raising=False)
        importlib.reload(conf)
        assert conf.LISTENER_CATCHUP_GAP_MINUTES == 30
        assert conf.LISTENER_PUMP_SLICE_SECONDS == 30
        importlib.reload(conf)
