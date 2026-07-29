from django.contrib import admin

from crm.models import Deal


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = (
        "lead",
        "campaign",
        "state",
        "invitation_sender",
        "invitation_sent_at",
        "invitation_withdrawn_at",
        "connected_at",
        "update_date",
    )
    list_filter = ("state", "closing_reason", "campaign", "invitation_sender")
    search_fields = (
        "lead__first_name",
        "lead__last_name",
        "lead__company_name",
        "lead__linkedin_url",
        "reason",
    )
    raw_id_fields = ("lead", "campaign")
    readonly_fields = (
        "invitation_sent_at",
        "invitation_sender",
        "invitation_withdrawn_at",
        "connected_at",
        "creation_date",
        "update_date",
    )
    date_hierarchy = "update_date"
