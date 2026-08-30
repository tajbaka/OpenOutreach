"""Exact sender-owned connection evidence for LinkedIn drip lanes."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable


@dataclass(frozen=True)
class LinkedInConnectionProof:
    deal_id: int
    operator: str
    mode: str
    invitation_sent_at: str = ""
    message_id: int | None = None
    message_external_id: str = ""

    def as_plan_value(self) -> dict:
        return asdict(self)


def _message_operator(message) -> str:
    from linkedin.operators import resolve_operator

    if message.operator_id:
        return resolve_operator(message.operator.handle)
    return resolve_operator(message.sender)


def _message_binds_deal_and_operator(message, *, deal_id: int, operator: str) -> bool:
    from linkedin.operators import resolve_operator

    parts = (message.external_id or "").split(":", 3)
    if len(parts) < 4 or parts[0] != "daemon-send":
        return False
    try:
        external_deal_id = int(parts[2])
    except (TypeError, ValueError):
        return False
    return (
        external_deal_id == deal_id
        and resolve_operator(parts[1]) == operator
        and _message_operator(message) == operator
    )


def connected_deal_proofs_by_operator(
    *,
    lead,
    operators: Iterable[str],
    allowed_states: Iterable[str],
) -> dict[str, tuple[LinkedInConnectionProof, ...]]:
    """Return positive proofs for canonical senders with two bounded queries.

    A connected Deal is attributed to a sender only by either the complete
    invitation ledger (timestamp + sender + no withdrawal), or a persisted
    outbound LinkedIn message whose canonical owner and synthetic external ID
    both bind that sender to that exact Deal.
    """
    from crm.models import Deal, Message
    from linkedin.operators import resolve_operator

    canonical_operators = {
        resolved
        for resolved in (resolve_operator(operator) for operator in operators)
        if resolved
    }
    deals = list(
        Deal.objects.filter(
            lead=lead,
            state__in=tuple(allowed_states),
        ).order_by("pk"),
    )
    if not deals:
        return {operator: () for operator in canonical_operators}

    outbound_messages = list(
        Message.objects.filter(
            lead=lead,
            source=Message.Source.LINKEDIN,
            direction=Message.Direction.OUTBOUND,
            external_id__startswith="daemon-send:",
        ).select_related("operator").order_by("pk"),
    )

    proof_by_owner_deal: dict[tuple[str, int], LinkedInConnectionProof] = {}
    for deal in deals:
        invitation_operator = resolve_operator(deal.invitation_sender)
        if (
            deal.invitation_sent_at is not None
            and deal.invitation_withdrawn_at is None
            and invitation_operator in canonical_operators
        ):
            proof_by_owner_deal[(invitation_operator, deal.pk)] = LinkedInConnectionProof(
                deal_id=deal.pk,
                operator=invitation_operator,
                mode="invitation_ledger",
                invitation_sent_at=deal.invitation_sent_at.isoformat(),
            )

    deal_ids = {deal.pk for deal in deals}
    for message in outbound_messages:
        parts = (message.external_id or "").split(":", 3)
        if len(parts) < 4 or parts[0] != "daemon-send":
            continue
        try:
            deal_id = int(parts[2])
        except (TypeError, ValueError):
            continue
        external_operator = resolve_operator(parts[1])
        if (
            deal_id not in deal_ids
            or external_operator not in canonical_operators
            or not _message_binds_deal_and_operator(
                message,
                deal_id=deal_id,
                operator=external_operator,
            )
        ):
            continue
        proof_by_owner_deal.setdefault(
            (external_operator, deal_id),
            LinkedInConnectionProof(
                deal_id=deal_id,
                operator=external_operator,
                mode="exact_outbound_message",
                message_id=message.pk,
                message_external_id=message.external_id,
            )
        )

    return {
        operator: tuple(
            proof
            for (proof_operator, _deal_id), proof in sorted(
                proof_by_owner_deal.items(),
                key=lambda item: (item[0][0], item[0][1]),
            )
            if proof_operator == operator
        )
        for operator in canonical_operators
    }


def sender_owned_connected_deal_proofs(
    *,
    lead,
    operator: str,
    allowed_states: Iterable[str],
) -> tuple[LinkedInConnectionProof, ...]:
    from linkedin.operators import resolve_operator

    canonical_operator = resolve_operator(operator)
    return connected_deal_proofs_by_operator(
        lead=lead,
        operators=(canonical_operator,),
        allowed_states=allowed_states,
    ).get(canonical_operator, ())
