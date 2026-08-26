from django.contrib import admin

from crm.models import (
    Account,
    Deal,
    MeetingNote,
    MeetingNoteSyncState,
    MeetingParticipant,
    Opportunity,
    OpportunityAction,
    OpportunityContact,
    OpportunitySheetState,
    OpportunityStageEvent,
    SalesOwner,
)


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


@admin.register(SalesOwner)
class SalesOwnerAdmin(admin.ModelAdmin):
    list_display = ("handle", "display_name", "active", "updated_at")
    list_filter = ("active",)
    search_fields = ("handle", "display_name")
    readonly_fields = ("normalized_handle", "created_at", "updated_at")


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ("name", "domain", "updated_at")
    search_fields = ("name", "normalized_name", "domain")
    readonly_fields = ("normalized_name", "created_at", "updated_at")


@admin.register(Opportunity)
class OpportunityAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "account",
        "owner",
        "stage",
        "sales_motion_step",
        "last_meaningful_activity_at",
        "updated_at",
    )
    list_filter = ("stage", "owner", "manual_pin", "source")
    search_fields = ("name", "account__name", "owner__handle")
    raw_id_fields = ("account", "owner")
    readonly_fields = (
        "id",
        "stage_entered_at",
        "human_revision",
        "created_at",
        "updated_at",
    )
    date_hierarchy = "updated_at"


@admin.register(OpportunityContact)
class OpportunityContactAdmin(admin.ModelAdmin):
    list_display = ("opportunity", "lead", "role", "is_primary")
    list_filter = ("role", "is_primary")
    search_fields = (
        "opportunity__name",
        "lead__first_name",
        "lead__last_name",
        "lead__linkedin_url",
    )
    raw_id_fields = ("opportunity", "lead")


@admin.register(OpportunityAction)
class OpportunityActionAdmin(admin.ModelAdmin):
    list_display = ("opportunity", "kind", "status", "due_on", "waiting_until")
    list_filter = ("kind", "status", "disposition")
    search_fields = ("opportunity__name", "description", "idempotency_key")
    raw_id_fields = ("opportunity", "trigger_message", "trigger_meeting")
    readonly_fields = ("id", "created_at", "updated_at")


@admin.register(OpportunityStageEvent)
class OpportunityStageEventAdmin(admin.ModelAdmin):
    list_display = ("opportunity", "from_stage", "to_stage", "source", "changed_at")
    list_filter = ("to_stage", "source")
    raw_id_fields = ("opportunity", "actor")
    readonly_fields = (
        "opportunity",
        "from_stage",
        "to_stage",
        "source",
        "actor",
        "changed_at",
        "created_at",
    )


@admin.register(OpportunitySheetState)
class OpportunitySheetStateAdmin(admin.ModelAdmin):
    list_display = ("opportunity", "published_revision", "last_published_at", "last_imported_at")
    raw_id_fields = ("opportunity",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(MeetingParticipant)
class MeetingParticipantAdmin(admin.ModelAdmin):
    list_display = ("meeting", "lead", "match_method", "is_primary")
    list_filter = ("match_method", "is_primary")
    search_fields = ("attendee_email", "attendee_name", "lead__linkedin_url")
    raw_id_fields = ("meeting", "lead")


@admin.register(MeetingNote)
class MeetingNoteAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "source",
        "detail_status",
        "match_status",
        "scheduled_start_at",
        "updated_at",
    )
    list_filter = ("source", "detail_status", "match_status", "match_method")
    search_fields = ("external_id", "title", "owner_email", "calendar_event_id")
    raw_id_fields = ("meeting", "opportunity")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MeetingNoteSyncState)
class MeetingNoteSyncStateAdmin(admin.ModelAdmin):
    list_display = ("source", "status", "successful_watermark", "last_success_at")
    list_filter = ("status", "source")
    readonly_fields = ("created_at", "updated_at")
