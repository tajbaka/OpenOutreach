"""Task-bound capability for the two automated Gmail delivery lanes."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Iterable

from gmail.auth import GMAIL_OPERATOR_MAPPING
from gmail.exceptions import GmailDeliveryAuthorizationError


_PERMIT_SEAL = object()


def _message_fingerprint(
    *,
    operator: str,
    account_key: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str,
    in_reply_to: str,
    references: Iterable[str],
    rfc_message_id: str,
) -> str:
    payload = {
        "operator": (operator or "").strip(),
        "account_key": (account_key or "").strip().lower(),
        "to": (to or "").strip().lower(),
        "subject": subject,
        "body": body,
        "thread_id": (thread_id or "").strip(),
        "in_reply_to": (in_reply_to or "").strip(),
        "references": [
            value.strip()
            for value in references
            if (value or "").strip()
        ],
        "rfc_message_id": (rfc_message_id or "").strip(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class GmailDeliveryPermit:
    """Single-use capability bound to one claimed Task and exact message."""

    task_id: int
    task_type: str
    _fingerprint: str = field(repr=False)
    _seal: object = field(repr=False)
    _used: bool = field(default=False, repr=False)


def issue_gmail_delivery_permit(
    *,
    task,
    operator: str,
    account_key: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str = "",
    in_reply_to: str = "",
    references: Iterable[str] = (),
    rfc_message_id: str = "",
) -> GmailDeliveryPermit:
    """Issue one permit for an already-claimed campaign or drip Gmail Task."""
    from linkedin.models import Task

    if not isinstance(task, Task) or task.pk is None:
        raise GmailDeliveryAuthorizationError(
            "Gmail delivery requires a persisted automation Task"
        )
    persisted = Task.objects.filter(pk=task.pk).values(
        "task_type",
        "status",
        "payload",
    ).first()
    if persisted is None:
        raise GmailDeliveryAuthorizationError("Gmail delivery Task no longer exists")
    if task.status != Task.Status.RUNNING or persisted["status"] != Task.Status.RUNNING:
        raise GmailDeliveryAuthorizationError(
            "Gmail delivery requires an already-claimed running Task"
        )
    allowed_types = {
        Task.TaskType.GMAIL_FOLLOW_UP,
        Task.TaskType.DRIP_GMAIL,
    }
    if task.task_type not in allowed_types or persisted["task_type"] not in allowed_types:
        raise GmailDeliveryAuthorizationError(
            "Gmail delivery Task is not an automated Gmail lane"
        )
    if task.task_type != persisted["task_type"]:
        raise GmailDeliveryAuthorizationError("Gmail delivery Task type changed")

    clean_operator = (operator or "").strip()
    payload = persisted["payload"] if isinstance(persisted["payload"], dict) else {}
    if not clean_operator or payload.get("operator") != clean_operator:
        raise GmailDeliveryAuthorizationError(
            "Gmail delivery operator does not own the claimed Task"
        )
    mapping = GMAIL_OPERATOR_MAPPING.get(clean_operator)
    if mapping is None or mapping["gmail_account"] != account_key:
        raise GmailDeliveryAuthorizationError(
            "Gmail delivery account does not match the claimed Task operator"
        )

    return GmailDeliveryPermit(
        task_id=task.pk,
        task_type=task.task_type,
        _fingerprint=_message_fingerprint(
            operator=clean_operator,
            account_key=account_key,
            to=to,
            subject=subject,
            body=body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            rfc_message_id=rfc_message_id,
        ),
        _seal=_PERMIT_SEAL,
    )


def consume_gmail_delivery_permit(
    permit: GmailDeliveryPermit | None,
    *,
    operator: str,
    account_key: str,
    to: str,
    subject: str,
    body: str,
    thread_id: str = "",
    in_reply_to: str = "",
    references: Iterable[str] = (),
    rfc_message_id: str = "",
) -> None:
    """Validate and consume a permit before any Gmail provider interaction."""
    if not isinstance(permit, GmailDeliveryPermit) or permit._seal is not _PERMIT_SEAL:
        raise GmailDeliveryAuthorizationError(
            "Direct Gmail sends are disabled; a claimed automation Task is required"
        )
    if permit._used:
        raise GmailDeliveryAuthorizationError("Gmail delivery permit was already used")
    fingerprint = _message_fingerprint(
        operator=operator,
        account_key=account_key,
        to=to,
        subject=subject,
        body=body,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
        rfc_message_id=rfc_message_id,
    )
    if fingerprint != permit._fingerprint:
        raise GmailDeliveryAuthorizationError(
            "Gmail delivery content does not match its claimed Task permit"
        )
    permit._used = True
