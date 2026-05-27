# linkedin/admin.py
from django.contrib import admin

from chat.models import ChatMessage

from linkedin.models import (
    ActionLog,
    Campaign,
    ConnectIssueLog,
    LinkedInProfile,
    OutreachSuppression,
    SearchKeyword,
    Task,
)


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = (
        "name", "user", "status", "booking_link", "is_freemium", "action_fraction",
    )
    list_filter = ("status", "is_freemium")
    raw_id_fields = ("user",)


@admin.register(OutreachSuppression)
class OutreachSuppressionAdmin(admin.ModelAdmin):
    list_display = ("kind", "value", "domain", "email", "active", "reason", "updated_at")
    list_filter = ("kind", "active")
    search_fields = ("value", "aliases", "domain", "email", "linkedin_url", "public_identifier", "reason")
    readonly_fields = ("normalized_value", "normalized_aliases", "created_at", "updated_at")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from linkedin.suppression import apply_suppression_to_existing_leads

        apply_suppression_to_existing_leads(obj)


@admin.register(LinkedInProfile)
class LinkedInProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "linkedin_username", "active", "legal_accepted")
    list_filter = ("active",)
    raw_id_fields = ("user",)


@admin.register(SearchKeyword)
class SearchKeywordAdmin(admin.ModelAdmin):
    list_display = ("keyword", "campaign", "used", "used_at")
    list_filter = ("used", "campaign")
    raw_id_fields = ("campaign",)


@admin.register(ActionLog)
class ActionLogAdmin(admin.ModelAdmin):
    list_display = ("action_type", "linkedin_profile", "campaign", "created_at")
    list_filter = ("action_type", "campaign")
    raw_id_fields = ("linkedin_profile", "campaign")
    date_hierarchy = "created_at"
    readonly_fields = ("linkedin_profile", "campaign", "action_type", "created_at")


@admin.register(ConnectIssueLog)
class ConnectIssueLogAdmin(admin.ModelAdmin):
    list_display = ("issue_type", "linkedin_profile", "campaign", "public_id", "created_at")
    list_filter = ("issue_type", "campaign", "linkedin_profile")
    search_fields = ("public_id", "profile_url", "reason")
    raw_id_fields = ("linkedin_profile", "campaign")
    date_hierarchy = "created_at"
    readonly_fields = (
        "linkedin_profile", "campaign", "public_id", "profile_url",
        "issue_type", "reason", "metadata", "created_at",
    )


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("task_type", "status", "scheduled_at", "payload", "created_at")
    list_filter = ("task_type", "status")
    readonly_fields = (
        "task_type", "status", "scheduled_at", "payload", "error",
        "created_at", "started_at", "completed_at",
    )
    date_hierarchy = "scheduled_at"


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("content_type", "object_id", "owner", "creation_date")
    list_filter = ("content_type", "owner")
    raw_id_fields = ("owner", "answer_to", "topic")
    date_hierarchy = "creation_date"
    readonly_fields = ("content_type", "object_id", "content", "owner", "creation_date")
