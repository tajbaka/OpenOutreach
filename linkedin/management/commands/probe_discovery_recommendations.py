"""Read-only live probe for personalized LinkedIn recommendation surfaces."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from linkedin.actions.search import search_profile
from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.browser.registry import get_or_create_session
from linkedin.discovery.browser_safety import assert_discovery_browser_available
from linkedin.discovery.config import discovery_limits, validate_discovery_settings
from linkedin.discovery.sources.mynetwork_recommendations import (
    collect_mynetwork_recommendations,
)
from linkedin.discovery.sources.profile_recommendations import (
    collect_profile_recommendations,
)
from linkedin.exceptions import DiscoverySessionConflictError, SkipProfile
from linkedin.models import LinkedInProfile
from linkedin.operators import resolve_operator


def _anonymous_identifier(value: str) -> str:
    return hashlib.blake2s(value.encode(), digest_size=8).hexdigest()


def _identity_name(profile: dict) -> str:
    return (
        (profile.get("full_name") or "").strip()
        or f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    )


class Command(BaseCommand):
    help = (
        "Probe My Network and one-hop profile recommendations in a saved "
        "sender session without database writes or outbound LinkedIn actions."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--handle",
            required=True,
            help="Django username for the LinkedInProfile to inspect.",
        )
        parser.add_argument(
            "--output",
            required=True,
            help="Local JSON artifact path for sanitized probe results.",
        )
        parser.add_argument(
            "--max-profile-probes",
            type=int,
            default=5,
            help="Maximum recommended profiles opened to find a More profiles rail.",
        )

    def handle(self, *args, **options):
        validate_discovery_settings()
        if options["max_profile_probes"] < 1:
            raise CommandError("--max-profile-probes must be at least 1.")
        try:
            linkedin_profile = LinkedInProfile.objects.select_related("user").get(
                active=True,
                user__username=options["handle"],
            )
        except LinkedInProfile.DoesNotExist as exc:
            raise CommandError("No matching active LinkedInProfile found.") from exc

        operator = resolve_operator(linkedin_profile.linkedin_username)
        try:
            assert_discovery_browser_available(
                operator=operator,
                account_username=linkedin_profile.linkedin_username,
            )
        except DiscoverySessionConflictError as exc:
            raise CommandError(str(exc)) from exc

        limits = discovery_limits()
        session = get_or_create_session(handle=linkedin_profile.user.username)
        session.campaign = session.campaigns.filter(status="active").first()
        profile_result = None
        source_profile = ""
        try:
            session.ensure_browser()
            api = PlaywrightLinkedinAPI(session=session)
            self_profile, _raw = api.get_profile(public_identifier="me")
            authenticated_name = _identity_name(self_profile or {})
            authenticated_operator = resolve_operator(authenticated_name)
            if authenticated_operator != operator:
                raise CommandError(
                    f"Authenticated LinkedIn identity {authenticated_name!r} maps to "
                    f"{authenticated_operator!r}, expected {operator!r}.",
                )

            network_result = collect_mynetwork_recommendations(
                session,
                max_cards=min(limits.max_cards, 80),
                max_sections=min(limits.max_sections, 6),
                max_scroll_rounds=min(limits.max_scroll_rounds, 8),
                max_consecutive_empty_scrolls=(
                    limits.max_consecutive_empty_scrolls
                ),
            )
            for card in network_result.cards[: options["max_profile_probes"]]:
                try:
                    search_profile(
                        session,
                        {
                            "url": card.linkedin_url,
                            "public_identifier": card.public_identifier,
                        },
                    )
                except SkipProfile:
                    continue
                candidate_result = collect_profile_recommendations(
                    session,
                    source_profile_public_identifier=card.public_identifier,
                    max_cards=min(
                        limits.max_profile_recommendations_per_visit,
                        30,
                    ),
                    max_scroll_rounds=min(limits.max_scroll_rounds, 6),
                    max_consecutive_empty_scrolls=(
                        limits.max_consecutive_empty_scrolls
                    ),
                )
                if candidate_result.sections_scanned:
                    profile_result = candidate_result
                    source_profile = card.public_identifier
                    break
        finally:
            session.close()

        if profile_result is None:
            raise CommandError(
                "No More profiles for you rail was found within the bounded "
                "profile probe.",
            )

        output = Path(options["output"]).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        artifact = {
            "operator": operator,
            "authenticated_operator": authenticated_operator,
            "mynetwork": {
                "sections": list(network_result.section_headings),
                "sections_scanned": network_result.sections_scanned,
                "scroll_rounds": network_result.scroll_rounds,
                "cards": len(network_result.cards),
                "stop_reason": network_result.stop_reason,
                "show_all_overlays_opened": network_result.overlays_opened,
                "sample_ids": [
                    _anonymous_identifier(card.public_identifier)
                    for card in network_result.cards[:10]
                ],
            },
            "profile_recommendations": {
                "source_id": _anonymous_identifier(source_profile),
                "sections": list(profile_result.section_headings),
                "scroll_rounds": profile_result.scroll_rounds,
                "cards": len(profile_result.cards),
                "stop_reason": profile_result.stop_reason,
                "show_all_overlays_opened": profile_result.overlays_opened,
                "sample_ids": [
                    _anonymous_identifier(card.public_identifier)
                    for card in profile_result.cards[:10]
                ],
            },
            "outbound_actions_clicked": 0,
            "database_writes": 0,
        }
        output.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
        self.stdout.write(
            self.style.SUCCESS(
                f"Verified recommendation sources for {operator}; "
                f"mynetwork_cards={len(network_result.cards)} "
                f"profile_cards={len(profile_result.cards)} output={output}",
            ),
        )
