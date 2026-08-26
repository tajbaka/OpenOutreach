"""Durable Trello pipeline identity and stage history.

Trello is a human pipeline surface, not the canonical account identity.  These
records make every card/list association explicit so sync never falls back to
matching mutable company names.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class OpportunityTrelloState(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.OneToOneField(
        "crm.Opportunity",
        on_delete=models.CASCADE,
        related_name="trello_state",
    )
    board_id = models.CharField(max_length=64)
    card_id = models.CharField(max_length=64, unique=True)
    list_id = models.CharField(max_length=64)
    published_pipeline_stage = models.CharField(max_length=32, blank=True, default="")
    published_card_snapshot = models.JSONField(blank=True, default=dict)
    trello_date_last_activity = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["board_id", "list_id"], name="crm_trello_board_list_idx"),
        ]

    def __str__(self):
        return f"trello-state opportunity={self.opportunity_id} card={self.card_id}"


class OpportunityPipelineEvent(models.Model):
    class Source(models.TextChoices):
        TRELLO = "trello", "Trello"
        MANUAL = "manual", "Manual"
        SYSTEM = "system", "System"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    opportunity = models.ForeignKey(
        "crm.Opportunity",
        on_delete=models.CASCADE,
        related_name="pipeline_events",
    )
    from_stage = models.CharField(max_length=32, blank=True, default="")
    to_stage = models.CharField(max_length=32, blank=True, default="")
    source = models.CharField(max_length=16, choices=Source.choices)
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["changed_at", "created_at", "id"]
        indexes = [
            models.Index(
                fields=["opportunity", "-changed_at"],
                name="crm_pipeline_opp_time_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.opportunity_id}: "
            f"{self.from_stage or 'radar'} → {self.to_stage or 'radar'}"
        )
