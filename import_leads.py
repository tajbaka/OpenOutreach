import os
import sys
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")
django.setup()

from linkedin.models import Campaign
from linkedin.setup.seeds import create_seed_leads_from_csv, parse_csv_leads

CAMPAIGN_PK = 1
CSV_PATH = "leads/sales_nav_7453236290411655169.csv"

campaign = Campaign.objects.get(pk=CAMPAIGN_PK)
text = open(CSV_PATH, encoding="utf-8").read()
leads = parse_csv_leads(text)

print(f"Parsed {len(leads)} CSV rows; importing into campaign #{CAMPAIGN_PK} {campaign.name!r}")

created = create_seed_leads_from_csv(campaign, leads, initial_state="Ready to Connect")
print(f"Imported {created} new leads as READY TO CONNECT (skipped {len(leads) - created} duplicates)")
