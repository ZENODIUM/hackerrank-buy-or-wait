"""Build a RequestContext: one question plus that user's related artifacts."""

from __future__ import annotations

from models import DataIndexes, ImageRef, Message, Request, RequestContext


def _unique_messages(messages: list[Message]) -> list[Message]:
    seen: set[str] = set()
    unique: list[Message] = []
    for message in messages:
        if message.message_id in seen:
            continue
        seen.add(message.message_id)
        unique.append(message)
    unique.sort(key=lambda item: (item.sent_at, item.message_id))
    return unique


def _unique_images(images: list[ImageRef]) -> list[ImageRef]:
    seen: set[str] = set()
    unique: list[ImageRef] = []
    for image in images:
        if image.image_id in seen:
            continue
        seen.add(image.image_id)
        unique.append(image)
    unique.sort(key=lambda item: item.image_id)
    return unique


def assemble_context(indexes: DataIndexes, request: Request) -> RequestContext:
    profile = indexes.profiles[request.user_id]
    events = list(indexes.events_by_user.get(request.user_id, []))
    events.sort(
        key=lambda item: (
            item.settlement_date or item.event_date,
            item.event_date,
            item.event_id,
        )
    )

    messages = list(indexes.messages_by_user.get(request.user_id, []))
    messages.extend(indexes.messages_by_request.get(request.request_id, []))
    for event in events:
        messages.extend(indexes.messages_by_event.get(event.event_id, []))

    images = list(indexes.images_by_user.get(request.user_id, []))
    images.extend(indexes.images_by_request.get(request.request_id, []))
    for event in events:
        if event.amount is None and event.event_id in indexes.images_by_event:
            images.append(indexes.images_by_event[event.event_id])

    return RequestContext(
        request=request,
        profile=profile,
        events=events,
        messages=_unique_messages(messages),
        images=_unique_images(images),
        options=list(indexes.options_by_request.get(request.request_id, [])),
        facts={},
    )


def resolve_request(indexes: DataIndexes, request_id: str) -> Request:
    if request_id in indexes.requests_by_id:
        return indexes.requests_by_id[request_id]
    if request_id in indexes.sample_requests_by_id:
        return indexes.sample_requests_by_id[request_id]
    raise KeyError(f"Unknown request_id: {request_id}")


def assemble_many(indexes: DataIndexes, requests: list[Request]) -> list[RequestContext]:
    return [assemble_context(indexes, request) for request in requests]
