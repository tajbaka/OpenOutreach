from django.contrib import admin, messages
from django.db import transaction
from django.utils import timezone

from drip.models import (
    NONTERMINAL_LANE_STATUSES,
    DripCampaign,
    DripCampaignVersion,
    DripDelivery,
    DripDeliveryAttempt,
    DripEnrollment,
    DripLane,
)


def _lock_lifecycle_for_lane_ids(lane_ids):
    """Lock one lane scope in canonical lifecycle order.

    The initial ID reads do not lock rows. Once the Lead rows are locked, all
    mutations follow Lead -> Enrollment -> Lane -> Delivery -> Attempt -> Task.
    Provider calls never occur in these Admin transactions.
    """
    from crm.models import Lead
    from linkedin.models import Task

    normalized_lane_ids = tuple(sorted({int(lane_id) for lane_id in lane_ids}))
    if not normalized_lane_ids:
        return {}, [], [], []

    seed_rows = list(
        DripLane.objects.filter(pk__in=normalized_lane_ids)
        .values_list("enrollment_id", "enrollment__lead_id"),
    )
    enrollment_ids = tuple(sorted({row[0] for row in seed_rows}))
    lead_ids = tuple(sorted({row[1] for row in seed_rows}))

    list(
        Lead.objects.select_for_update()
        .filter(pk__in=lead_ids)
        .order_by("pk")
        .values_list("pk", flat=True),
    )
    enrollments = list(
        DripEnrollment.objects.select_for_update()
        .filter(pk__in=enrollment_ids)
        .order_by("pk"),
    )
    lanes = list(
        DripLane.objects.select_for_update()
        .filter(pk__in=normalized_lane_ids)
        .order_by("pk"),
    )
    deliveries = list(
        DripDelivery.objects.select_for_update()
        .filter(lane_id__in=normalized_lane_ids)
        .order_by("pk"),
    )
    delivery_ids = [delivery.pk for delivery in deliveries]
    list(
        DripDeliveryAttempt.objects.select_for_update()
        .filter(delivery_id__in=delivery_ids)
        .order_by("pk")
        .values_list("pk", flat=True),
    )
    task_ids = sorted(
        {
            delivery.current_task_id
            for delivery in deliveries
            if delivery.current_task_id is not None
        },
    )
    tasks = list(
        Task.objects.select_for_update()
        .filter(pk__in=task_ids)
        .order_by("pk"),
    )
    return {enrollment.pk: enrollment for enrollment in enrollments}, lanes, deliveries, tasks


def _retire_pending_tasks(*, deliveries, tasks, now, reason, stop_deliveries=False):
    """Retire only Tasks that remain unclaimed under the acquired Task lock."""
    from linkedin.models import Task

    pending_task_ids = {
        task.pk for task in tasks if task.status == Task.Status.PENDING
    }
    if pending_task_ids:
        Task.objects.filter(pk__in=pending_task_ids).update(
            status=Task.Status.COMPLETED,
            completed_at=now,
            error=reason,
        )

    changed_deliveries = []
    for delivery in deliveries:
        if stop_deliveries and delivery.status in {
            DripDelivery.Status.PLANNED,
            DripDelivery.Status.QUEUED,
        }:
            delivery.status = DripDelivery.Status.STOPPED
            delivery.updated_at = now
            changed_deliveries.append(delivery)
        if delivery.current_task_id not in pending_task_ids:
            continue
        delivery.current_task_id = None
        if not stop_deliveries and delivery.status in {
            DripDelivery.Status.PLANNED,
            DripDelivery.Status.QUEUED,
        }:
            delivery.status = DripDelivery.Status.PLANNED
        delivery.updated_at = now
        if delivery not in changed_deliveries:
            changed_deliveries.append(delivery)

    if changed_deliveries:
        DripDelivery.objects.bulk_update(
            changed_deliveries,
            ("status", "current_task", "updated_at"),
        )
    return len(pending_task_ids)


class ReadOnlyAuditAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DripCampaign)
class DripCampaignAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "status", "active_version", "updated_at")
    list_filter = ("status",)
    search_fields = ("key", "name")
    readonly_fields = ("key", "active_version", "created_at", "updated_at")
    actions = ("pause_campaigns", "resume_campaigns")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Pause selected drip campaigns")
    def pause_campaigns(self, request, queryset):
        from crm.models import Lead

        selected_ids = tuple(sorted(queryset.values_list("pk", flat=True)))
        now = timezone.now()
        paused_count = 0
        retired_count = 0
        with transaction.atomic():
            campaigns = list(
                DripCampaign.objects.select_for_update()
                .filter(pk__in=selected_ids)
                .order_by("pk"),
            )
            active_ids = {
                campaign.pk
                for campaign in campaigns
                if campaign.status == DripCampaign.Status.ACTIVE
            }
            enrollment_seed = list(
                DripEnrollment.objects.filter(campaign_id__in=active_ids)
                .values_list("pk", "lead_id"),
            )
            enrollment_ids = tuple(sorted({row[0] for row in enrollment_seed}))
            lead_ids = tuple(sorted({row[1] for row in enrollment_seed}))
            list(
                Lead.objects.select_for_update()
                .filter(pk__in=lead_ids)
                .order_by("pk")
                .values_list("pk", flat=True),
            )
            list(
                DripEnrollment.objects.select_for_update()
                .filter(pk__in=enrollment_ids)
                .order_by("pk")
                .values_list("pk", flat=True),
            )
            lane_ids = list(
                DripLane.objects.select_for_update()
                .filter(enrollment_id__in=enrollment_ids)
                .order_by("pk")
                .values_list("pk", flat=True),
            )
            deliveries = list(
                DripDelivery.objects.select_for_update()
                .filter(lane_id__in=lane_ids)
                .order_by("pk"),
            )
            delivery_ids = [delivery.pk for delivery in deliveries]
            list(
                DripDeliveryAttempt.objects.select_for_update()
                .filter(delivery_id__in=delivery_ids)
                .order_by("pk")
                .values_list("pk", flat=True),
            )

            from linkedin.models import Task

            task_ids = sorted(
                {
                    delivery.current_task_id
                    for delivery in deliveries
                    if delivery.current_task_id is not None
                },
            )
            tasks = list(
                Task.objects.select_for_update()
                .filter(pk__in=task_ids)
                .order_by("pk"),
            )
            retired_count = _retire_pending_tasks(
                deliveries=deliveries,
                tasks=tasks,
                now=now,
                reason="Drip campaign paused in Admin",
            )
            for campaign in campaigns:
                if campaign.pk not in active_ids:
                    continue
                campaign.status = DripCampaign.Status.PAUSED
                campaign.updated_at = now
                campaign.save(update_fields={"status", "updated_at"})
                paused_count += 1
        self.message_user(
            request,
            f"Paused {paused_count} campaign(s); retired {retired_count} pending drip Task(s).",
            messages.SUCCESS,
        )

    @admin.action(description="Resume selected paused drip campaigns")
    def resume_campaigns(self, request, queryset):
        now = timezone.now()
        count = queryset.filter(
            status=DripCampaign.Status.PAUSED,
            active_version__isnull=False,
        ).update(status=DripCampaign.Status.ACTIVE, updated_at=now)
        self.message_user(request, f"Resumed {count} campaign(s).", messages.SUCCESS)


@admin.register(DripCampaignVersion)
class DripCampaignVersionAdmin(ReadOnlyAuditAdmin):
    list_display = ("campaign", "version", "content_hash", "published_at")
    list_filter = ("campaign",)
    search_fields = ("campaign__key", "campaign__name", "content_hash")
    raw_id_fields = ("campaign",)

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DripEnrollment)
class DripEnrollmentAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "lead",
        "campaign",
        "campaign_version",
        "frozen_icp",
        "status",
        "activated_at",
        "stopped_at",
    )
    list_filter = ("status", "campaign", "frozen_icp")
    search_fields = (
        "lead__first_name",
        "lead__last_name",
        "lead__company_name",
        "lead__email",
        "lead__linkedin_url",
        "plan_hash",
    )
    raw_id_fields = (
        "lead",
        "campaign",
        "campaign_version",
        "stop_trigger_message",
        "stop_trigger_meeting",
    )
    readonly_fields = tuple(field.name for field in DripEnrollment._meta.fields)
    actions = ("pause_enrollments", "resume_enrollments", "stop_enrollments")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Pause selected drip enrollments")
    def pause_enrollments(self, request, queryset):
        selected_ids = set(queryset.values_list("pk", flat=True))
        lane_ids = tuple(
            DripLane.objects.filter(enrollment_id__in=selected_ids).values_list(
                "pk", flat=True,
            ),
        )
        now = timezone.now()
        count = retired_count = 0
        with transaction.atomic():
            enrollments, _lanes, deliveries, tasks = _lock_lifecycle_for_lane_ids(
                lane_ids,
            )
            for enrollment in enrollments.values():
                if enrollment.pk not in selected_ids or enrollment.status not in {
                    DripEnrollment.Status.WAITING,
                    DripEnrollment.Status.ACTIVE,
                }:
                    continue
                enrollment.status = DripEnrollment.Status.PAUSED
                enrollment.save(update_fields={"status", "updated_at"})
                count += 1
            selected_delivery_ids = {
                delivery.pk
                for delivery in deliveries
                if delivery.lane.enrollment_id in selected_ids
            }
            retired_count = _retire_pending_tasks(
                deliveries=[d for d in deliveries if d.pk in selected_delivery_ids],
                tasks=tasks,
                now=now,
                reason="Drip enrollment paused in Admin",
            )
        self.message_user(
            request,
            f"Paused {count} enrollment(s); retired {retired_count} pending drip Task(s).",
            messages.SUCCESS,
        )

    @admin.action(description="Resume selected paused drip enrollments")
    def resume_enrollments(self, request, queryset):
        selected_ids = set(queryset.values_list("pk", flat=True))
        lane_ids = tuple(
            DripLane.objects.filter(enrollment_id__in=selected_ids).values_list(
                "pk", flat=True,
            ),
        )
        count = 0
        with transaction.atomic():
            enrollments, lanes, _deliveries, _tasks = _lock_lifecycle_for_lane_ids(
                lane_ids,
            )
            for enrollment in enrollments.values():
                # The locked recheck prevents a concurrent inbound stop from
                # being overwritten back to ACTIVE.
                if enrollment.pk not in selected_ids or enrollment.status != DripEnrollment.Status.PAUSED:
                    continue
                has_waiting_lane = any(
                    lane.enrollment_id == enrollment.pk
                    and lane.status in {
                        DripLane.Status.WAITING_CURRENT,
                        DripLane.Status.WAITING_CONNECTION,
                    }
                    for lane in lanes
                )
                enrollment.status = (
                    DripEnrollment.Status.WAITING
                    if has_waiting_lane
                    else DripEnrollment.Status.ACTIVE
                )
                enrollment.save(update_fields={"status", "updated_at"})
                count += 1
        self.message_user(request, f"Resumed {count} enrollment(s).", messages.SUCCESS)

    @admin.action(description="Stop selected drip enrollments")
    def stop_enrollments(self, request, queryset):
        selected_ids = set(queryset.values_list("pk", flat=True))
        lane_ids = tuple(
            DripLane.objects.filter(enrollment_id__in=selected_ids).values_list(
                "pk", flat=True,
            ),
        )
        now = timezone.now()
        count = 0
        with transaction.atomic():
            enrollments, lanes, deliveries, tasks = _lock_lifecycle_for_lane_ids(
                lane_ids,
            )
            for enrollment in enrollments.values():
                if enrollment.pk not in selected_ids or enrollment.status in {
                    DripEnrollment.Status.STOPPED,
                    DripEnrollment.Status.COMPLETED,
                }:
                    continue
                enrollment.status = DripEnrollment.Status.STOPPED
                enrollment.stopped_at = now
                enrollment.stop_reason = "admin_stop"
                enrollment.stop_detail = f"Stopped in Admin by {request.user.get_username()}"
                enrollment.save(update_fields={
                    "status", "stopped_at", "stop_reason", "stop_detail", "updated_at",
                })
                count += 1
            for lane in lanes:
                if lane.enrollment_id in selected_ids and lane.status in NONTERMINAL_LANE_STATUSES:
                    lane.status = DripLane.Status.STOPPED
                    lane.save(update_fields={"status", "updated_at"})
            _retire_pending_tasks(
                deliveries=[d for d in deliveries if d.lane.enrollment_id in selected_ids],
                tasks=tasks,
                now=now,
                reason="Stopped by drip Admin",
                stop_deliveries=True,
            )
        self.message_user(request, f"Stopped {count} enrollment(s).", messages.SUCCESS)


@admin.register(DripLane)
class DripLaneAdmin(ReadOnlyAuditAdmin):
    list_display = (
        "id",
        "enrollment",
        "channel",
        "operator",
        "provider_account",
        "status",
        "current_sequence_status",
        "handed_off_at",
        "current_theme_key",
    )
    list_filter = ("channel", "status", "current_sequence_status", "operator")
    search_fields = (
        "enrollment__lead__first_name",
        "enrollment__lead__last_name",
        "provider_account",
        "sender_identity",
        "recipient_identity",
        "gmail_thread_id",
    )
    raw_id_fields = ("enrollment",)
    actions = ("pause_lanes", "resume_lanes", "stop_lanes")

    @admin.action(description="Pause selected drip lanes")
    def pause_lanes(self, request, queryset):
        lane_ids = tuple(sorted(queryset.values_list("pk", flat=True)))
        now = timezone.now()
        paused_count = 0
        retired_count = 0
        with transaction.atomic():
            _enrollments, lanes, deliveries, tasks = _lock_lifecycle_for_lane_ids(
                lane_ids,
            )
            selected_ids = set(lane_ids)
            for lane in lanes:
                if lane.pk not in selected_ids or lane.status not in {
                    DripLane.Status.WAITING_CURRENT,
                    DripLane.Status.WAITING_CONNECTION,
                    DripLane.Status.ACTIVE,
                }:
                    continue
                lane.status = DripLane.Status.PAUSED
                lane.updated_at = now
                lane.save(update_fields={"status", "updated_at"})
                paused_count += 1
            paused_lane_ids = {
                lane.pk for lane in lanes if lane.status == DripLane.Status.PAUSED
            }
            affected_deliveries = [
                delivery
                for delivery in deliveries
                if delivery.lane_id in paused_lane_ids
            ]
            affected_task_ids = {
                delivery.current_task_id
                for delivery in affected_deliveries
                if delivery.current_task_id is not None
            }
            retired_count = _retire_pending_tasks(
                deliveries=affected_deliveries,
                tasks=[task for task in tasks if task.pk in affected_task_ids],
                now=now,
                reason="Drip lane paused in Admin",
            )
        self.message_user(
            request,
            f"Paused {paused_count} lane(s); retired {retired_count} pending drip Task(s).",
            messages.SUCCESS,
        )

    @admin.action(description="Resume selected paused drip lanes")
    def resume_lanes(self, request, queryset):
        lane_ids = tuple(sorted(queryset.values_list("pk", flat=True)))
        resumed_count = 0
        unclear_count = 0
        parent_inactive_count = 0
        with transaction.atomic():
            enrollments, lanes, deliveries, _tasks = _lock_lifecycle_for_lane_ids(
                lane_ids,
            )
            campaign_by_id = DripCampaign.objects.in_bulk(
                {enrollment.campaign_id for enrollment in enrollments.values()},
            )
            unclear_lane_ids = {
                delivery.lane_id
                for delivery in deliveries
                if delivery.status == DripDelivery.Status.UNCLEAR
            }
            for lane in lanes:
                if lane.status != DripLane.Status.PAUSED:
                    continue
                enrollment = enrollments[lane.enrollment_id]
                campaign = campaign_by_id[enrollment.campaign_id]
                if lane.pk in unclear_lane_ids:
                    unclear_count += 1
                    continue
                if (
                    campaign.status != DripCampaign.Status.ACTIVE
                    or enrollment.status
                    not in {DripEnrollment.Status.WAITING, DripEnrollment.Status.ACTIVE}
                ):
                    parent_inactive_count += 1
                    continue
                lane.status = (
                    DripLane.Status.ACTIVE
                    if lane.handed_off_at is not None
                    else DripLane.Status.WAITING_CURRENT
                )
                lane.save(update_fields={"status", "updated_at"})
                resumed_count += 1
        self.message_user(
            request,
            f"Resumed {resumed_count} lane(s).",
            messages.SUCCESS,
        )
        if unclear_count:
            self.message_user(
                request,
                f"Skipped {unclear_count} lane(s) with an UNCLEAR delivery. "
                "After human review, explicitly stop the lane for human takeover; "
                "it cannot be returned to automation.",
                messages.WARNING,
            )
        if parent_inactive_count:
            self.message_user(
                request,
                f"Skipped {parent_inactive_count} lane(s) whose campaign or enrollment is inactive.",
                messages.WARNING,
            )

    @admin.action(description="Stop selected drip lanes (human takeover)")
    def stop_lanes(self, request, queryset):
        lane_ids = tuple(sorted(queryset.values_list("pk", flat=True)))
        now = timezone.now()
        stopped_count = 0
        unclear_count = 0
        retired_count = 0
        with transaction.atomic():
            _enrollments, lanes, deliveries, tasks = _lock_lifecycle_for_lane_ids(
                lane_ids,
            )
            for lane in lanes:
                if lane.status not in NONTERMINAL_LANE_STATUSES:
                    continue
                if any(
                    delivery.lane_id == lane.pk
                    and delivery.status == DripDelivery.Status.UNCLEAR
                    for delivery in deliveries
                ):
                    unclear_count += 1
                lane.status = DripLane.Status.STOPPED
                lane.updated_at = now
                lane.save(update_fields={"status", "updated_at"})
                stopped_count += 1
            stopped_lane_ids = {
                lane.pk for lane in lanes if lane.status == DripLane.Status.STOPPED
            }
            affected_deliveries = [
                delivery
                for delivery in deliveries
                if delivery.lane_id in stopped_lane_ids
            ]
            affected_task_ids = {
                delivery.current_task_id
                for delivery in affected_deliveries
                if delivery.current_task_id is not None
            }
            retired_count = _retire_pending_tasks(
                deliveries=affected_deliveries,
                tasks=[task for task in tasks if task.pk in affected_task_ids],
                now=now,
                reason=(
                    f"Drip lane stopped in Admin by {request.user.get_username()} "
                    "for human takeover"
                ),
                stop_deliveries=True,
            )
        detail = (
            f"Stopped {stopped_count} lane(s); retired {retired_count} pending drip Task(s)."
        )
        if unclear_count:
            detail += (
                f" {unclear_count} lane(s) retained UNCLEAR delivery evidence for audit."
            )
        self.message_user(request, detail, messages.SUCCESS)


@admin.register(DripDelivery)
class DripDeliveryAdmin(ReadOnlyAuditAdmin):
    list_display = (
        "id",
        "lane",
        "theme_key",
        "step_index",
        "status",
        "scheduled_at",
        "sent_at",
        "current_task",
    )
    list_filter = ("status", "lane__channel", "provider_account")
    search_fields = (
        "provider_message_id",
        "provider_thread_id",
        "rfc_message_id",
        "lane__recipient_identity",
    )
    raw_id_fields = ("lane", "current_task", "outbound_message")


@admin.register(DripDeliveryAttempt)
class DripDeliveryAttemptAdmin(ReadOnlyAuditAdmin):
    list_display = (
        "id",
        "delivery",
        "attempt_number",
        "outcome",
        "started_at",
        "submission_attempted_at",
        "finished_at",
    )
    list_filter = ("outcome",)
    search_fields = ("delivery__provider_message_id", "diagnostic_detail")
    raw_id_fields = ("delivery",)
