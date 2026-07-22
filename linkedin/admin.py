# linkedin/admin.py
from django.contrib import admin

from chat.models import ChatMessage

from linkedin.models import (
    ActionLog,
    Campaign,
    ConnectIssueLog,
    FedRAMPMarketplaceSignal,
    FedRAMPMarketplaceSourceState,
    LinkedInFeedCollectionJob,
    LinkedInFeedObservation,
    LinkedInFeedPost,
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


@admin.register(LinkedInFeedCollectionJob)
class LinkedInFeedCollectionJobAdmin(admin.ModelAdmin):
    list_display = (
        "operator", "account_username", "collection_date", "status",
        "scheduled_for", "posts_seen", "posts_created", "observations_created",
    )
    list_filter = ("status", "operator")
    search_fields = ("operator", "account_username", "error")
    readonly_fields = (
        "operator", "account_username", "collection_date", "status",
        "scheduled_for", "started_at", "finished_at", "error",
        "posts_seen", "posts_created", "observations_created",
        "created_at", "updated_at",
    )
    date_hierarchy = "scheduled_for"


@admin.register(LinkedInFeedPost)
class LinkedInFeedPostAdmin(admin.ModelAdmin):
    list_display = (
        "author_name", "intent", "audience", "analyzed_at",
        "slack_notified_at", "last_seen_at", "post_url",
    )
    list_filter = ("intent", "audience", "analyzed_at", "slack_notified_at")
    search_fields = (
        "activity_urn", "post_url", "author_name", "author_headline",
        "post_text", "relevance_reason", "suggested_action",
    )
    readonly_fields = (
        "activity_urn", "post_url", "content_hash", "author_name",
        "author_headline", "author_profile_url", "post_text", "posted_at",
        "raw_payload", "analyzed_at", "intent", "audience", "topics",
        "relevance_reason", "suggested_action", "raw_analysis",
        "slack_notified_at", "first_seen_at", "last_seen_at", "created_at",
        "updated_at",
    )
    date_hierarchy = "last_seen_at"


@admin.register(LinkedInFeedObservation)
class LinkedInFeedObservationAdmin(admin.ModelAdmin):
    list_display = ("operator", "account_username", "post", "last_seen_at", "seen_count")
    list_filter = ("operator",)
    raw_id_fields = ("post", "job")
    readonly_fields = (
        "post", "job", "operator", "account_username",
        "first_seen_at", "last_seen_at", "seen_count",
    )
    date_hierarchy = "last_seen_at"


@admin.register(FedRAMPMarketplaceSourceState)
class FedRAMPMarketplaceSourceStateAdmin(admin.ModelAdmin):
    list_display = (
        "source_name", "source_exported_at", "last_polled_at", "content_sha256",
    )
    search_fields = ("source_name", "source_url", "content_sha256")
    readonly_fields = (
        "source_name", "source_url", "content_sha256", "source_exported_at",
        "snapshot", "last_polled_at", "created_at", "updated_at",
    )


@admin.register(FedRAMPMarketplaceSignal)
class FedRAMPMarketplaceSignalAdmin(admin.ModelAdmin):
    list_display = (
        "provider_name", "offering_name", "signal_type", "priority",
        "analyzed_at", "slack_notified_at", "recorded_at",
    )
    list_filter = (
        "signal_type", "source_kind", "priority", "is_relevant",
        "should_alert", "analyzed_at", "slack_notified_at",
    )
    search_fields = (
        "event_key", "source_event_id", "product_id", "provider_name",
        "offering_name", "relevance_reason", "suggested_action",
    )
    readonly_fields = (
        "event_key", "source_kind", "source_event_id", "signal_type",
        "icp_bucket", "product_id", "provider_name", "offering_name",
        "certification_path", "from_status", "to_status", "transition_at",
        "recorded_at", "source_url", "marketplace_url", "product_context",
        "raw_payload", "analyzed_at", "is_relevant", "should_alert",
        "priority", "relevance_reason", "suggested_action", "raw_analysis",
        "slack_notified_at", "first_seen_at", "last_seen_at", "created_at",
        "updated_at",
    )
    date_hierarchy = "recorded_at"


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
