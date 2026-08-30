from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Max

from drip.exceptions import PublicationError
from drip.manifest import ValidatedManifest
from drip.models import DripCampaign, DripCampaignVersion


@dataclass(frozen=True)
class PublicationPreview:
    campaign_key: str
    content_hash: str
    existing_version: int | None
    next_version: int


@dataclass(frozen=True)
class PublicationResult:
    campaign: DripCampaign
    version: DripCampaignVersion
    created: bool


def preview_publication(manifest: ValidatedManifest) -> PublicationPreview:
    campaign = DripCampaign.objects.filter(key=manifest.campaign_key).first()
    if campaign is None:
        return PublicationPreview(
            campaign_key=manifest.campaign_key,
            content_hash=manifest.content_hash,
            existing_version=None,
            next_version=1,
        )
    existing_version = campaign.versions.filter(
        content_hash=manifest.content_hash,
    ).values_list("version", flat=True).first()
    latest = campaign.versions.aggregate(value=Max("version"))["value"] or 0
    return PublicationPreview(
        campaign_key=manifest.campaign_key,
        content_hash=manifest.content_hash,
        existing_version=existing_version,
        next_version=latest + 1,
    )


@transaction.atomic
def publish_manifest(
    manifest: ValidatedManifest,
    *,
    activate: bool = True,
) -> PublicationResult:
    campaign, _created = DripCampaign.objects.get_or_create(
        key=manifest.campaign_key,
        defaults={
            "name": manifest.name,
            "status": DripCampaign.Status.DRAFT,
        },
    )
    campaign = DripCampaign.objects.select_for_update().get(pk=campaign.pk)
    if campaign.status == DripCampaign.Status.RETIRED:
        raise PublicationError(
            f"Campaign {campaign.key!r} is retired and cannot receive another version.",
        )

    version = campaign.versions.filter(content_hash=manifest.content_hash).first()
    created = version is None
    if version is None:
        latest = campaign.versions.aggregate(value=Max("version"))["value"] or 0
        version = DripCampaignVersion(
            campaign=campaign,
            version=latest + 1,
            manifest=manifest.normalized,
            content_hash=manifest.content_hash,
        )
        version.full_clean()
        version.save()

    campaign.name = manifest.name
    update_fields = {"name", "updated_at"}
    if activate:
        campaign.active_version = version
        update_fields.add("active_version")
        if campaign.status == DripCampaign.Status.DRAFT:
            campaign.status = DripCampaign.Status.ACTIVE
            update_fields.add("status")
    campaign.full_clean()
    campaign.save(update_fields=update_fields)
    return PublicationResult(campaign=campaign, version=version, created=created)
