"""Drop legacy CRM-pointer columns. Apply only after backfill_sheets is
verified in the Google Sheets People tab — once dropped, there's no path
back to the Attio/Airtable record IDs without restoring from a backup.

This migration is paired with the deletion of:
- linkedin/notifications/attio.py
- linkedin/notifications/airtable.py
- linkedin/management/commands/sync_attio.py
- linkedin/management/commands/sync_airtable.py
- linkedin/management/commands/backfill_airtable.py
- ATTIO_API_KEY / ATTIO_SALES_LIST_ID / AIRTABLE_* from linkedin/conf.py and .env
- AttioError + AirtableError from linkedin/exceptions.py
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0007_add_lead_airtable_ids"),
    ]

    operations = [
        migrations.RemoveField(model_name="lead", name="attio_person_id"),
        migrations.RemoveField(model_name="lead", name="attio_company_id"),
        migrations.RemoveField(model_name="lead", name="attio_entry_id"),
        migrations.RemoveField(model_name="lead", name="airtable_person_record_id"),
        migrations.RemoveField(model_name="lead", name="airtable_company_record_id"),
        migrations.RemoveField(model_name="lead", name="airtable_deal_record_id"),
    ]
