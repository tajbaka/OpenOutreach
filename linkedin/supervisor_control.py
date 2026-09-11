"""Read persistent stops and consume sender-scoped supervisor restart requests."""

from __future__ import annotations

import logging
from collections.abc import Callable


logger = logging.getLogger(__name__)


def read_sender_stop(*, linkedin_username: str) -> bool:
    """Read one exact local sender's persistent stop latch without mutating it.

    An absent or ambiguous local identity is not evidence that starting workers
    is safe. The caller must fail closed on ``SupervisorControlError`` or DB
    failure. No operator alias, active-profile, or Django username fallback is
    permitted here.
    """
    from linkedin.exceptions import SupervisorControlError
    from linkedin.models import LinkedInProfile

    username = linkedin_username.strip()
    if not username:
        raise SupervisorControlError("Local LinkedIn username is missing for the stop check.")

    values = list(
        LinkedInProfile.objects.filter(linkedin_username__iexact=username)
        .order_by("pk").values_list("stop_requested", flat=True)[:2]
    )
    if len(values) != 1:
        raise SupervisorControlError(
            "Local LinkedIn username matches "
            f"{'multiple' if values else 'no'} profiles for the stop check."
        )
    return values[0]


def consume_sender_restart(
    *, linkedin_username: str, restart: Callable[[], bool],
) -> bool:
    """Restart one exact local sender and acknowledge only confirmed success.

    The caller initializes Django and owns process shutdown/startup. The row
    lock remains held while ``restart`` runs; competing consumers skip it and
    a new request writer waits until acknowledgement, preserving that request.
    Only an explicit ``True`` callback result acknowledges the request. DB and
    callback errors propagate, rolling back acknowledgement on failure.
    """
    username = linkedin_username.strip()
    if not username:
        logger.warning("Skipping supervisor restart check: local LinkedIn username is missing")
        return False

    # Keep this module importable by the standalone supervisor before setup.
    from django.db import transaction

    from linkedin.models import LinkedInProfile

    with transaction.atomic():
        matches = LinkedInProfile.objects.filter(linkedin_username__iexact=username)
        profile_ids = list(matches.order_by("pk").values_list("pk", flat=True)[:2])
        if len(profile_ids) != 1:
            logger.warning(
                "Skipping supervisor restart check: local LinkedIn username matches %s profiles",
                "multiple" if profile_ids else "no",
            )
            return False

        # Check uniqueness before skipping locked rows: one locked duplicate
        # must not make another matching sender appear unambiguous.
        profile = (
            matches.filter(pk=profile_ids[0], restart_requested=True, stop_requested=False)
            .select_for_update(of=("self",), no_key=True, skip_locked=True)
            .only("pk", "restart_requested", "stop_requested")
            .first()
        )
        if profile is None:
            return False

        if restart() is not True:
            return False

        LinkedInProfile.objects.filter(pk=profile.pk).update(restart_requested=False)
        return True
