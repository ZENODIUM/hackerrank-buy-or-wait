"""Typed records for the Buy or Wait? data layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@dataclass(slots=True)
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: Decimal
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass(slots=True)
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: Decimal
    minimum_balance_to_keep: Decimal
    financial_priorities: list[str]
    protect: set[str]
    can_reduce: set[str]
    can_stop: set[str]
    methods: set[str]
    max_installment_months: int | None


@dataclass(slots=True)
class Event:
    event_id: str
    user_id: str
    event_type: str
    description: str
    category: str
    direction: str
    amount: Decimal | None
    currency: str
    event_date: date
    settlement_date: date | None
    status: str
    linked_event_id: str | None
    flexibility: str
    minimum_allowed_amount: Decimal | None


@dataclass(slots=True)
class Message:
    message_id: str
    user_id: str
    request_id: str | None
    related_event_id: str | None
    sent_at: str
    source_type: str
    message_text: str


@dataclass(slots=True)
class ImageRef:
    image_id: str
    user_id: str
    request_id: str | None
    related_event_id: str | None
    path: Path
    exists: bool


@dataclass(slots=True)
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: str
    payment_amount: Decimal
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: int | None
    financing_fee: Decimal
    total_payable_amount: Decimal


@dataclass(slots=True)
class DataIndexes:
    repo_root: Path
    profiles: dict[str, Profile]
    events_by_id: dict[str, Event]
    events_by_user: dict[str, list[Event]]
    messages_by_id: dict[str, Message]
    messages_by_user: dict[str, list[Message]]
    messages_by_request: dict[str, list[Message]]
    messages_by_event: dict[str, list[Message]]
    images_by_id: dict[str, ImageRef]
    images_by_user: dict[str, list[ImageRef]]
    images_by_request: dict[str, list[ImageRef]]
    images_by_event: dict[str, ImageRef]
    options_by_request: dict[str, list[PaymentOption]]
    fx: dict[tuple[date, str, str], Decimal]
    requests: list[Request]
    requests_by_id: dict[str, Request]
    sample_requests: list[Request]
    sample_requests_by_id: dict[str, Request]

    def stats(self) -> dict[str, int]:
        return {
            "profiles": len(self.profiles),
            "events": len(self.events_by_id),
            "messages": len(self.messages_by_id),
            "images": len(self.images_by_id),
            "payment_options": sum(len(v) for v in self.options_by_request.values()),
            "fx_rates": len(self.fx),
            "eval_requests": len(self.requests),
            "sample_requests": len(self.sample_requests),
        }


@dataclass
class RequestContext:
    """One request plus every artifact needed to decide it."""

    request: Request
    profile: Profile
    events: list[Event]
    messages: list[Message]
    images: list[ImageRef]
    options: list[PaymentOption]
    facts: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        blank_amounts = [e.event_id for e in self.events if e.amount is None]
        return {
            "request_id": self.request.request_id,
            "user_id": self.request.user_id,
            "request_date": self.request.request_date.isoformat(),
            "requested_amount": str(self.request.requested_amount),
            "home_currency": self.profile.home_currency,
            "current_available_balance": str(self.profile.current_available_balance),
            "minimum_balance_to_keep": str(self.profile.minimum_balance_to_keep),
            "methods": sorted(self.profile.methods),
            "max_installment_months": self.profile.max_installment_months,
            "event_count": len(self.events),
            "blank_amount_events": blank_amounts,
            "message_count": len(self.messages),
            "image_count": len(self.images),
            "option_count": len(self.options),
            "missing_image_files": [i.image_id for i in self.images if not i.exists],
        }

    def to_full_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "request": _json_safe(asdict(self.request)),
            "profile": _json_safe(asdict(self.profile)),
            "messages": [_json_safe(asdict(m)) for m in self.messages],
            "images": [_json_safe(asdict(i)) for i in self.images],
            "options": [_json_safe(asdict(o)) for o in self.options],
            "facts": _json_safe(self.facts),
            "events": [_json_safe(asdict(e)) for e in self.events],
        }

    def to_debug_dict(self, event_limit: int = 8) -> dict[str, Any]:
        payload = self.to_full_dict()
        payload["events_preview"] = payload["events"][:event_limit]
        del payload["events"]
        return payload
