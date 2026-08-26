import django.db.models.deletion
from django.db import migrations, models


def backfill_action_targets(apps, schema_editor):
    """Route legacy actions only when an Opportunity contact proves identity."""
    OpportunityAction = apps.get_model("crm", "OpportunityAction")
    OpportunityContact = apps.get_model("crm", "OpportunityContact")

    actions = OpportunityAction.objects.filter(target_lead__isnull=True).select_related(
        "trigger_message",
        "trigger_meeting",
    )
    for action in actions.iterator():
        contact_rows = list(
            OpportunityContact.objects.filter(opportunity_id=action.opportunity_id)
            .values("lead_id", "role", "is_primary")
            .order_by("id")
        )
        contact_ids = {row["lead_id"] for row in contact_rows}
        candidate_id = None

        if action.trigger_message_id is not None:
            trigger_lead_id = action.trigger_message.lead_id
            if trigger_lead_id in contact_ids:
                candidate_id = trigger_lead_id
            else:
                # An explicit but contradictory trigger must not be rerouted
                # to a convenient account contact.
                continue
        elif action.trigger_meeting_id is not None:
            trigger_lead_id = action.trigger_meeting.lead_id
            if trigger_lead_id in contact_ids:
                candidate_id = trigger_lead_id
            else:
                continue

        if (
            candidate_id is None
            and action.trigger_message_id is None
            and action.trigger_meeting_id is None
        ):
            for role in ("champion", "decision_maker"):
                role_ids = {
                    row["lead_id"]
                    for row in contact_rows
                    if row["role"] == role
                }
                if len(role_ids) == 1:
                    candidate_id = next(iter(role_ids))
                    break
        if candidate_id is None:
            primary_ids = {
                row["lead_id"]
                for row in contact_rows
                if row["is_primary"]
            }
            if len(primary_ids) == 1:
                candidate_id = next(iter(primary_ids))
        if candidate_id is None and len(contact_ids) == 1:
            candidate_id = next(iter(contact_ids))

        if candidate_id is not None:
            OpportunityAction.objects.filter(pk=action.pk, target_lead__isnull=True).update(
                target_lead_id=candidate_id,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0017_opportunity_action_human_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="opportunityaction",
            name="target_lead",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="targeted_opportunity_actions",
                to="crm.lead",
            ),
        ),
        migrations.RunPython(backfill_action_targets, migrations.RunPython.noop),
    ]
