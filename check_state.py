import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")
django.setup()

from collections import Counter
from crm.models import Deal, Lead
from linkedin.models import Campaign

print("=== State counts ===")
print(Counter(d.state for d in Deal.objects.all()))

print("\n=== Next 5 RTC deals (by Deal.id) — what daemon would hit next ===")
for d in Deal.objects.filter(state="Ready to Connect").order_by("id")[:5]:
    print(f"  deal_id={d.id} lead_id={d.lead.pk} {d.lead.linkedin_url}")

print("\n=== First 5 RTC deals from your CSV import (latest insertions) ===")
for d in Deal.objects.filter(state="Ready to Connect").order_by("-id")[:5]:
    print(f"  deal_id={d.id} lead_id={d.lead.pk} {d.lead.linkedin_url}")
