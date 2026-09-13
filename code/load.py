"""Read the original dataset CSVs into in-memory indexes. Files are not modified."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from models import (
    DataIndexes,
    Event,
    ImageRef,
    Message,
    PaymentOption,
    Profile,
    Request,
)


def parse_pipe(value: str) -> list[str]:
    if not value or not value.strip():
        return []
    return [part.strip() for part in value.split("|") if part.strip()]


def parse_decimal(value: str) -> Decimal | None:
    if value is None or not str(value).strip():
        return None
    try:
        return Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal: {value!r}") from exc


def parse_date(value: str) -> date | None:
    if not value or not str(value).strip():
        return None
    return date.fromisoformat(value.strip())


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _require_decimal(value: str, field_name: str) -> Decimal:
    parsed = parse_decimal(value)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def _require_date(value: str, field_name: str) -> date:
    parsed = parse_date(value)
    if parsed is None:
        raise ValueError(f"{field_name} is required")
    return parsed


def load_profiles(rows: list[dict[str, str]]) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    for row in rows:
        months = row.get("max_installment_months", "").strip()
        profiles[row["user_id"]] = Profile(
            user_id=row["user_id"],
            home_currency=row["home_currency"],
            current_available_balance=_require_decimal(
                row["current_available_balance"], "current_available_balance"
            ),
            minimum_balance_to_keep=_require_decimal(
                row["minimum_balance_to_keep"], "minimum_balance_to_keep"
            ),
            financial_priorities=parse_pipe(row.get("financial_priorities", "")),
            protect=set(parse_pipe(row.get("expense_categories_to_protect", ""))),
            can_reduce=set(parse_pipe(row.get("expense_categories_user_is_willing_to_reduce", ""))),
            can_stop=set(parse_pipe(row.get("expense_categories_user_is_willing_to_stop", ""))),
            methods=set(parse_pipe(row.get("payment_methods_user_will_consider", ""))),
            max_installment_months=int(months) if months else None,
        )
    return profiles


def load_events(rows: list[dict[str, str]]) -> tuple[dict[str, Event], dict[str, list[Event]]]:
    by_id: dict[str, Event] = {}
    by_user: dict[str, list[Event]] = defaultdict(list)
    for row in rows:
        linked = row.get("linked_event_id", "").strip() or None
        event = Event(
            event_id=row["event_id"],
            user_id=row["user_id"],
            event_type=row["event_type"],
            description=row["description"],
            category=row["category"],
            direction=row["direction"],
            amount=parse_decimal(row.get("amount", "")),
            currency=row["currency"],
            event_date=_require_date(row["event_date"], "event_date"),
            settlement_date=parse_date(row.get("settlement_date", "")),
            status=row["status"],
            linked_event_id=linked,
            flexibility=row.get("flexibility", ""),
            minimum_allowed_amount=parse_decimal(row.get("minimum_allowed_amount", "")),
        )
        by_id[event.event_id] = event
        by_user[event.user_id].append(event)
    return by_id, dict(by_user)


def load_messages(
    rows: list[dict[str, str]],
) -> tuple[dict[str, Message], dict[str, list[Message]], dict[str, list[Message]], dict[str, list[Message]]]:
    by_id: dict[str, Message] = {}
    by_user: dict[str, list[Message]] = defaultdict(list)
    by_request: dict[str, list[Message]] = defaultdict(list)
    by_event: dict[str, list[Message]] = defaultdict(list)
    for row in rows:
        request_id = row.get("request_id", "").strip() or None
        related_event_id = row.get("related_event_id", "").strip() or None
        message = Message(
            message_id=row["message_id"],
            user_id=row["user_id"],
            request_id=request_id,
            related_event_id=related_event_id,
            sent_at=row.get("sent_at", ""),
            source_type=row.get("source_type", ""),
            message_text=row.get("message_text", ""),
        )
        by_id[message.message_id] = message
        by_user[message.user_id].append(message)
        if request_id:
            by_request[request_id].append(message)
        if related_event_id:
            by_event[related_event_id].append(message)
    return by_id, dict(by_user), dict(by_request), dict(by_event)


def load_images(
    rows: list[dict[str, str]], media_dir: Path
) -> tuple[dict[str, ImageRef], dict[str, list[ImageRef]], dict[str, list[ImageRef]], dict[str, ImageRef]]:
    by_id: dict[str, ImageRef] = {}
    by_user: dict[str, list[ImageRef]] = defaultdict(list)
    by_request: dict[str, list[ImageRef]] = defaultdict(list)
    by_event: dict[str, ImageRef] = {}
    for row in rows:
        request_id = row.get("request_id", "").strip() or None
        related_event_id = row.get("related_event_id", "").strip() or None
        path = media_dir / f"{row['image_id']}.png"
        image = ImageRef(
            image_id=row["image_id"],
            user_id=row["user_id"],
            request_id=request_id,
            related_event_id=related_event_id,
            path=path,
            exists=path.is_file(),
        )
        by_id[image.image_id] = image
        by_user[image.user_id].append(image)
        if request_id:
            by_request[request_id].append(image)
        if related_event_id:
            by_event[related_event_id] = image
    return by_id, dict(by_user), dict(by_request), by_event


def load_options(rows: list[dict[str, str]]) -> dict[str, list[PaymentOption]]:
    by_request: dict[str, list[PaymentOption]] = defaultdict(list)
    for row in rows:
        freq = row.get("payment_frequency_days", "").strip()
        option = PaymentOption(
            payment_option_id=row["payment_option_id"],
            request_id=row["request_id"],
            payment_method=row["payment_method"],
            payment_amount=_require_decimal(row["payment_amount"], "payment_amount"),
            number_of_payments=int(row["number_of_payments"]),
            first_payment_date=_require_date(row["first_payment_date"], "first_payment_date"),
            payment_frequency_days=int(freq) if freq else None,
            financing_fee=_require_decimal(row["financing_fee"], "financing_fee"),
            total_payable_amount=_require_decimal(row["total_payable_amount"], "total_payable_amount"),
        )
        by_request[option.request_id].append(option)
    return dict(by_request)


def load_fx(rows: list[dict[str, str]]) -> dict[tuple[date, str, str], Decimal]:
    rates: dict[tuple[date, str, str], Decimal] = {}
    for row in rows:
        key = (
            _require_date(row["rate_date"], "rate_date"),
            row["from_currency"],
            row["to_currency"],
        )
        rates[key] = _require_decimal(row["rate"], "rate")
    return rates


def load_requests(rows: list[dict[str, str]]) -> list[Request]:
    requests: list[Request] = []
    for row in rows:
        requests.append(
            Request(
                request_id=row["request_id"],
                user_id=row["user_id"],
                request_date=_require_date(row["request_date"], "request_date"),
                request_type=row["request_type"],
                requested_amount=_require_decimal(row["requested_amount"], "requested_amount"),
                desired_completion_date=_require_date(
                    row["desired_completion_date"], "desired_completion_date"
                ),
                allows_partial_payment=parse_bool(row["allows_partial_payment"]),
                request_text=row.get("request_text", ""),
            )
        )
    return requests


def load_indexes(repo_root: Path) -> DataIndexes:
    dataset = repo_root / "dataset"
    media_dir = dataset / "media" / "images"

    profiles = load_profiles(_read_csv(dataset / "financial_profiles.csv"))
    events_by_id, events_by_user = load_events(_read_csv(dataset / "financial_events.csv"))
    messages_by_id, messages_by_user, messages_by_request, messages_by_event = load_messages(
        _read_csv(dataset / "messages.csv")
    )
    images_by_id, images_by_user, images_by_request, images_by_event = load_images(
        _read_csv(dataset / "images.csv"), media_dir
    )
    options_by_request = load_options(_read_csv(dataset / "request_payment_options.csv"))
    fx = load_fx(_read_csv(dataset / "exchange_rates.csv"))
    requests = load_requests(_read_csv(dataset / "requests.csv"))
    sample_requests = load_requests(_read_csv(dataset / "sample_requests.csv"))

    return DataIndexes(
        repo_root=repo_root,
        profiles=profiles,
        events_by_id=events_by_id,
        events_by_user=events_by_user,
        messages_by_id=messages_by_id,
        messages_by_user=messages_by_user,
        messages_by_request=messages_by_request,
        messages_by_event=messages_by_event,
        images_by_id=images_by_id,
        images_by_user=images_by_user,
        images_by_request=images_by_request,
        images_by_event=images_by_event,
        options_by_request=options_by_request,
        fx=fx,
        requests=requests,
        requests_by_id={item.request_id: item for item in requests},
        sample_requests=sample_requests,
        sample_requests_by_id={item.request_id: item for item in sample_requests},
    )
