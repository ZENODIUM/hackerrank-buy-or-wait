"""Write data-layer artifacts to code/data_layer/ for inspection."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from assemble import assemble_many
from models import DataIndexes, RequestContext, _json_safe


def data_layer_dir(repo_root: Path) -> Path:
    path = repo_root / "code" / "data_layer"
    path.mkdir(parents=True, exist_ok=True)
    (path / "contexts").mkdir(exist_ok=True)
    return path


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def persist_indexes(indexes: DataIndexes) -> Path:
    folder = data_layer_dir(indexes.repo_root)
    blank_events = [
        {
            "event_id": event.event_id,
            "user_id": event.user_id,
            "description": event.description,
            "currency": event.currency,
            "image_id": indexes.images_by_event[event.event_id].image_id
            if event.event_id in indexes.images_by_event
            else None,
        }
        for event in indexes.events_by_id.values()
        if event.amount is None
    ]
    payload = {
        "stats": indexes.stats(),
        "blank_amount_events": blank_events,
        "eval_request_ids": [item.request_id for item in indexes.requests],
        "sample_request_ids": [item.request_id for item in indexes.sample_requests],
        "profiles": {user_id: _json_safe(asdict(profile)) for user_id, profile in indexes.profiles.items()},
        "requests": [_json_safe(asdict(item)) for item in indexes.requests],
        "sample_requests": [_json_safe(asdict(item)) for item in indexes.sample_requests],
        "messages": [_json_safe(asdict(item)) for item in indexes.messages_by_id.values()],
        "images": [_json_safe(asdict(item)) for item in indexes.images_by_id.values()],
        "payment_options": [
            _json_safe(asdict(option))
            for options in indexes.options_by_request.values()
            for option in options
        ],
        "exchange_rates": [
            {
                "rate_date": rate_date.isoformat(),
                "from_currency": from_currency,
                "to_currency": to_currency,
                "rate": str(rate),
            }
            for (rate_date, from_currency, to_currency), rate in indexes.fx.items()
        ],
    }
    path = folder / "indexes.json"
    _write_json(path, payload)
    return path


def persist_contexts(indexes: DataIndexes, contexts: list[RequestContext] | None = None) -> Path:
    folder = data_layer_dir(indexes.repo_root)
    if contexts is None:
        contexts = assemble_many(indexes, indexes.sample_requests + indexes.requests)

    index_rows = []
    for context in contexts:
        path = folder / "contexts" / f"{context.request.request_id}.json"
        _write_json(path, context.to_full_dict())
        index_rows.append(context.summary())

    index_path = folder / "contexts_index.json"
    _write_json(index_path, index_rows)
    return index_path


def persist_one_context(indexes: DataIndexes, context: RequestContext) -> Path:
    folder = data_layer_dir(indexes.repo_root)
    path = folder / "contexts" / f"{context.request.request_id}.json"
    _write_json(path, context.to_full_dict())
    return path
