"""One-off: normalize non-canonical Lead.linkedin_url values.

Leads imported via old CSV paths kept the raw URL (often missing the
trailing slash), so the daemon's URL-keyed Deal lookups — which build the
canonical form via public_id_to_url() — miss them. Fixes the data:

  - non-colliding non-canonical Lead -> rename linkedin_url to canonical.
  - collision (canonical form already a separate Lead) -> merge the
    non-canonical Lead INTO the canonical one (move deals/messages/
    meetings, backfill empty canonical fields), then delete it.

Run:  .venv/bin/python fix_linkedin_urls.py --dry-run
      .venv/bin/python fix_linkedin_urls.py
"""
import os
import sys

os.environ["DJANGO_SETTINGS_MODULE"] = "linkedin.django_settings"

import django

django.setup()

from django.db import transaction

from crm.models import Deal, Lead, Meeting, Message
from linkedin.db.urls import public_id_to_url, url_to_public_id

DRY = "--dry-run" in sys.argv

_BACKFILL_FIELDS = (
    "first_name", "last_name", "company_name", "email",
    "public_identifier", "description", "embedding", "icp",
)


def merge_lead(stale: Lead, canonical: Lead) -> None:
    """Move all related rows from `stale` onto `canonical`, then delete `stale`."""
    canon_campaigns = set(
        Deal.objects.filter(lead=canonical).values_list("campaign_id", flat=True)
    )
    for deal in Deal.objects.filter(lead=stale):
        if deal.campaign_id in canon_campaigns:
            raise RuntimeError(
                f"campaign collision: lead {stale.pk} and {canonical.pk} both "
                f"have a Deal in campaign {deal.campaign_id} — manual review"
            )

    moved_deals = Deal.objects.filter(lead=stale).count()
    moved_msgs = Message.objects.filter(lead=stale).count()
    moved_meetings = Meeting.objects.filter(lead=stale).count()

    backfilled = {}
    for fld in _BACKFILL_FIELDS:
        if not getattr(canonical, fld) and getattr(stale, fld):
            backfilled[fld] = getattr(stale, fld)

    print(f"  MERGE  stale pk={stale.pk} -> canonical pk={canonical.pk} "
          f"(deals={moved_deals}, msgs={moved_msgs}, meetings={moved_meetings}, "
          f"backfill={list(backfilled)})")

    if DRY:
        return

    with transaction.atomic():
        Deal.objects.filter(lead=stale).update(lead=canonical)
        Message.objects.filter(lead=stale).update(lead=canonical)
        Meeting.objects.filter(lead=stale).update(lead=canonical)
        if backfilled:
            for fld, val in backfilled.items():
                setattr(canonical, fld, val)
            canonical.save(update_fields=list(backfilled))
        stale.delete()


def main() -> None:
    renamed = merged = 0
    for lead in Lead.objects.all().order_by("pk"):
        url = lead.linkedin_url or ""
        if not url:
            continue
        pid = url_to_public_id(url)
        if not pid:
            continue
        canon = public_id_to_url(pid)
        if canon == url:
            continue

        other = Lead.objects.filter(linkedin_url=canon).exclude(pk=lead.pk).first()
        if other is None:
            print(f"  RENAME pk={lead.pk}  {url!r} -> {canon!r}")
            if not DRY:
                lead.linkedin_url = canon
                lead.save(update_fields=["linkedin_url"])
            renamed += 1
        else:
            merge_lead(lead, other)
            merged += 1

    mode = "DRY-RUN — no writes" if DRY else "APPLIED"
    print(f"\n{mode}: {renamed} renamed, {merged} merged")


if __name__ == "__main__":
    main()
