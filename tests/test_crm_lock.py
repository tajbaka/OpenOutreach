from unittest.mock import MagicMock, patch

import pytest
from django.db import connections

from linkedin.crm_lock import CrmRefreshAlreadyRunning, crm_refresh_lock


@pytest.mark.django_db
def test_postgres_crm_lock_rejects_concurrent_refresh():
    connection = MagicMock()
    connection.vendor = "postgresql"
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (False,)

    with (
        patch.object(connections["default"], "copy", return_value=connection),
        pytest.raises(CrmRefreshAlreadyRunning, match="already running"),
    ):
        with crm_refresh_lock():
            pass

    connection.close.assert_called_once()


@pytest.mark.django_db
def test_postgres_crm_lock_releases_after_failure():
    connection = MagicMock()
    connection.vendor = "postgresql"
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (True,)

    with (
        patch.object(connections["default"], "copy", return_value=connection),
        pytest.raises(RuntimeError, match="boom"),
    ):
        with crm_refresh_lock():
            raise RuntimeError("boom")

    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements == [
        "SELECT pg_try_advisory_lock(%s)",
        "SELECT pg_advisory_unlock(%s)",
    ]
    connection.close.assert_called_once()


@pytest.mark.django_db
def test_local_crm_lock_rejects_reentry():
    with crm_refresh_lock():
        with pytest.raises(CrmRefreshAlreadyRunning, match="already running"):
            with crm_refresh_lock():
                pass
