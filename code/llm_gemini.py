"""Gemini client. Default model is the highest free-tier daily-limit Flash-Lite."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Highest published free-tier RPD (~500/day) as of Sep 2026.
# Override with GEMINI_MODEL if your project does not have this id yet.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
)

USAGE: dict[str, Any] = {
    "provider": "google",
    "model": None,
    "calls": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "failures": 0,
    "errors": [],
}

# Paid-tier default 1.0s. Free-tier Flash-Lite is ~15 RPM — set
# GEMINI_MIN_INTERVAL_SEC=4.5 if you are back on free quota. 429 retries
# still sleep 20s/40s so either tier works.
_last_call_at = 0.0


def min_interval_sec() -> float:
    raw = os.environ.get("GEMINI_MIN_INTERVAL_SEC", "1.0")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1.0


def _throttle() -> None:
    global _last_call_at
    wait = min_interval_sec() - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _unique_models() -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in (model_name(), *FALLBACK_MODELS):
        if name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text


def _is_missing_model(exc: Exception) -> bool:
    text = str(exc).lower()
    return "404" in text or "not found" in text or "not supported" in text


def load_env(repo_root: Path | None = None) -> None:
    if repo_root is not None:
        load_dotenv(repo_root / ".env")
    load_dotenv()


def api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def model_name() -> str:
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


def _record_usage(response: Any, used_model: str) -> None:
    USAGE["model"] = used_model
    USAGE["calls"] += 1
    meta = getattr(response, "usage_metadata", None)
    if meta is not None:
        USAGE["input_tokens"] += int(getattr(meta, "prompt_token_count", 0) or 0)
        USAGE["output_tokens"] += int(getattr(meta, "candidates_token_count", 0) or 0)
    _persist_usage()


def _persist_usage() -> None:
    """Keep last live totals so a cache-only rerun does not look like an API outage."""
    folder = Path(__file__).resolve().parent / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "usage.json").write_text(json.dumps(USAGE, indent=2), encoding="utf-8")


def last_live_usage() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "cache" / "usage.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _client():
    key = api_key()
    if not key:
        return None
    from google import genai

    return genai.Client(api_key=key)


def generate_text(prompt: str, repo_root: Path | None = None) -> str | None:
    load_env(repo_root)
    client = _client()
    if client is None:
        USAGE["failures"] += 1
        USAGE["errors"].append("missing_api_key")
        return None
    last_error = None
    for name in _unique_models():
        for attempt in range(3):
            _throttle()
            try:
                response = client.models.generate_content(model=name, contents=prompt)
                _record_usage(response, name)
                return (response.text or "").strip()
            except Exception as exc:  # noqa: BLE001
                last_error = f"{name}: {exc}"
                if _is_rate_limit(exc) and attempt < 2:
                    time.sleep(20 * (attempt + 1))
                    continue
                if _is_missing_model(exc):
                    break
                break
    USAGE["failures"] += 1
    USAGE["errors"].append(last_error)
    return None


def generate_json(prompt: str, repo_root: Path | None = None) -> dict[str, Any] | None:
    text = generate_text(prompt, repo_root)
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        USAGE["failures"] += 1
        USAGE["errors"].append("json_parse_failed")
        return None
    if not isinstance(parsed, dict):
        USAGE["failures"] += 1
        USAGE["errors"].append("json_not_object")
        return None
    return parsed


def read_image_amount(image_path: Path, description: str, repo_root: Path | None = None) -> str | None:
    load_env(repo_root)
    client = _client()
    if client is None:
        USAGE["failures"] += 1
        USAGE["errors"].append("missing_api_key")
        return None
    from google.genai import types

    prompt = (
        "Read this bill/receipt/payslip. Return JSON only: "
        '{"amount": "<number>", "label": "<which line>"}. '
        f"The financial event is: {description}. "
        "Use the net/payable/grand-total/outstanding amount that matches that event. "
        "No currency symbols. No extra text."
    )
    raw = image_path.read_bytes()
    suffix = image_path.suffix.lower()
    mime = "image/png" if suffix == ".png" else "image/jpeg"
    last_error = None
    for name in _unique_models():
        for attempt in range(3):
            _throttle()
            try:
                response = client.models.generate_content(
                    model=name,
                    contents=[
                        types.Part.from_bytes(data=raw, mime_type=mime),
                        prompt,
                    ],
                )
                _record_usage(response, name)
                text = (response.text or "").strip()
                if text.startswith("```"):
                    text = text.strip("`").replace("json", "", 1).strip()
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    start = text.find("{")
                    end = text.rfind("}")
                    data = json.loads(text[start : end + 1]) if start >= 0 and end > start else {}
                amount = data.get("amount") if isinstance(data, dict) else None
                if amount in (None, "") and text:
                    import re

                    match = re.search(r"\d[\d.,]{1,14}", text)
                    amount = match.group(0) if match else None
                return str(amount) if amount not in (None, "") else None
            except Exception as exc:  # noqa: BLE001
                last_error = f"{name}: {exc}"
                if _is_rate_limit(exc) and attempt < 2:
                    time.sleep(20 * (attempt + 1))
                    continue
                if _is_missing_model(exc):
                    break
                break
    USAGE["failures"] += 1
    USAGE["errors"].append(last_error)
    return None


def usage_snapshot() -> dict[str, Any]:
    return dict(USAGE)
