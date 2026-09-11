"""Consume sender-scoped restart requests for the local process supervisor."""

from __future__ import annotations

import logging
from collections.abc import Callable


logger = logging.getLogger(__name__)


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
            matches.filter(pk=profile_ids[0], restart_requested=True)
            .select_for_update(of=("self",), no_key=True, skip_locked=True)
            .only("pk", "restart_requested")
            .first()
        )
        if profile is None:
            return False

        if restart() is not True:
            return False

        LinkedInProfile.objects.filter(pk=profile.pk).update(restart_requested=False)
        return True
