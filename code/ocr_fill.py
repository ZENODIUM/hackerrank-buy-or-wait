"""Fill blank event amounts from linked images using RapidOCR."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from models import DataIndexes, Event, ImageRef, RequestContext

CACHE_NAME = "ocr.json"


def cache_path(repo_root: Path) -> Path:
    folder = repo_root / "code" / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / CACHE_NAME


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_cache(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _get_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:  # pragma: no cover
        from rapidocr import RapidOCR
    return RapidOCR()


def run_ocr(image: ImageRef, engine) -> list[dict[str, Any]]:
    result, _elapsed = engine(str(image.path))
    lines: list[dict[str, Any]] = []
    if not result:
        return lines
    for item in result:
        box, text, confidence = item[0], item[1], float(item[2])
        top = float(box[0][1])
        left = float(box[0][0])
        lines.append(
            {
                "text": str(text),
                "confidence": confidence,
                "top": top,
                "left": left,
            }
        )
    lines.sort(key=lambda row: (row["top"], row["left"]))
    return lines


_MONEY_TOKEN = re.compile(
    r"(?:(?:INR|IDR|ZAR|EUR|USD|RS|₹|\$)\s*)?"
    r"\d{1,3}(?:[.,]\d{2,3})+(?:[.,]\d{2})?"
    r"|\d+[.,]\d{2}",
    re.IGNORECASE,
)


def parse_money(raw: str) -> Decimal | None:
    token = re.sub(r"(?i)(?:inr|idr|zar|eur|usd|rs\.?|₹|\$)", "", raw)
    token = token.strip().replace(" ", "")
    if not token:
        return None
    digits = re.sub(r"\D", "", token)
    if len(digits) > 10:
        return None
    try:
        if re.fullmatch(r"\d+,\d{2}", token):
            left, _right = token.split(",")
            if len(left) >= 6:
                return None
            return Decimal(token.replace(",", "."))
        if re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d{2}", token):
            return Decimal(token.replace(".", "").replace(",", "."))
        if "." in token:
            decimals = token.rsplit(".", 1)[1]
            if len(decimals) == 2:
                return Decimal(token.replace(",", ""))
            if len(decimals) == 3 and "," not in token:
                return Decimal(token.replace(".", ""))
        if "," in token:
            parsed = Decimal(token.replace(",", ""))
        else:
            parsed = Decimal(token)
    except InvalidOperation:
        return None
    if parsed == parsed.to_integral() and 1900 <= int(parsed) <= 2100:
        return None
    return parsed


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _label_score(label: str, event: Event) -> int:
    blob = _norm(label)
    desc = event.description.lower()
    category = event.category.lower()
    score = 0

    if any(
        key in blob
        for key in (
            "grandtotal",
            "totaltotal",
            "totalpaid",
            "totalpaidid",
            "totalamountreceived",
            "netamount",
            "netpayable",
            "netpay",
            "amountpayable",
            "amountdue",
            "balancedue",
            "itembill",
            "itembillno",
        )
    ):
        score += 8
    if "subtotal" in blob:
        score -= 8
    elif "total" in blob:
        score += 4
    if any(key in blob for key in ("subtotal", "mrp", "taxable", "cgst", "sgst", "previousbalance")):
        score -= 6
    if any(key in blob for key in ("cashpaid", "cashpaidid", "changegiven", "change")):
        score -= 4

    if "salary" in desc or "net" in desc:
        if any(key in blob for key in ("netpayable", "netpay", "netamount")):
            score += 12
        if "totalearning" in blob or "gross" in blob:
            score -= 8
    if "outstanding" in desc or "balance" in desc:
        if any(key in blob for key in ("balancedue", "outstanding", "amountdue", "amountreceived")):
            score += 10
        if "totalamounttobereceived" in blob or "totaltobereceived" in blob:
            score -= 2
    if "after" in blob or "amountdueafter" in blob:
        score -= 16
    if "payable" in desc or "due" in desc:
        if "after" not in blob and any(key in blob for key in ("amountpayable", "amountduetill", "amountdue", "balancedue")):
            score += 10
    if "taxi" in desc or "fare" in desc:
        if blob.endswith("total") or blob == "total":
            score += 8
        if "cash" in blob:
            score -= 8
    if "telecom" in desc or category == "utilities":
        if "amountdue" in blob or "thismonthscharges" in blob:
            score += 8
    if category == "salary" and "net" in blob:
        score += 6
    return score


@dataclass(slots=True)
class AmountHit:
    amount: Decimal
    label: str
    raw: str
    score: int


def extract_hits(lines: list[dict[str, Any]], event: Event) -> list[AmountHit]:
    hits: list[AmountHit] = []
    texts = [row["text"] for row in lines]
    for index, row in enumerate(texts):
        previous = " ".join(texts[max(0, index - 3) : index])
        window = f"{previous} {row}"
        prev_norm = _norm(texts[index - 1]) if index else ""
        in_footer = index >= max(1, int(len(texts) * 0.65))
        if in_footer and prev_norm in {"total", "grandtotal", "totaltotal", "netpay", "netpayable"}:
            cluster: list[Decimal] = []
            for follow in texts[index : min(len(texts), index + 4)]:
                follow_norm = _norm(follow)
                if any(token in follow_norm for token in ("cash", "change", "tender")):
                    break
                parsed = parse_money(follow) or (
                    Decimal(follow.strip()) if re.fullmatch(r"\d+(?:\.\d{1,2})?", follow.strip()) else None
                )
                if parsed and parsed > 0:
                    cluster.append(parsed)
            if cluster:
                best = max(cluster)
                hits.append(AmountHit(amount=best, label=window, raw=str(best), score=22))
        if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}", row):
            continue
        for match in _MONEY_TOKEN.finditer(row):
            amount = parse_money(match.group(0))
            if amount is None or amount <= 0:
                continue
            label = window
            hits.append(
                AmountHit(
                    amount=amount,
                    label=label,
                    raw=match.group(0),
                    score=_label_score(label, event),
                )
            )
        if re.fullmatch(r"\d{3,8}", row.strip()):
            amount = Decimal(row.strip())
            score = _label_score(window, event)
            if score > 0:
                hits.append(AmountHit(amount=amount, label=window, raw=row.strip(), score=score + 4))
    return hits


def choose_amount(event: Event, hits: list[AmountHit]) -> Decimal | None:
    if not hits:
        return None
    labeled = [hit for hit in hits if hit.score > 0]
    pool = labeled or hits
    ranked = sorted(pool, key=lambda hit: (hit.score, hit.amount), reverse=True)
    return ranked[0].amount


def fill_blank_amounts(indexes: DataIndexes, contexts: list[RequestContext]) -> dict[str, Any]:
    path = cache_path(indexes.repo_root)
    cache = _load_cache(path)
    engine = None
    filled: list[dict[str, Any]] = []
    unresolved: list[str] = []

    blank_events = [
        event
        for event in indexes.events_by_id.values()
        if event.amount is None and event.event_id in indexes.images_by_event
    ]

    for event in blank_events:
        image = indexes.images_by_event[event.event_id]
        cached = cache.get(image.image_id)
        if cached and cached.get("lines"):
            lines = [
                {"text": text, "confidence": 1.0, "top": index, "left": 0}
                for index, text in enumerate(cached["lines"])
            ]
            source = "cache"
        elif not image.exists:
            unresolved.append(event.event_id)
            continue
        else:
            if engine is None:
                engine = _get_engine()
            lines = run_ocr(image, engine)
            source = "rapidocr"

        hits = extract_hits(lines, event)
        amount = choose_amount(event, hits)
        if amount is None and cached and cached.get("gemini_amount"):
            amount = Decimal(str(cached["gemini_amount"]))
            source = "gemini_cache"
        previous = cached if isinstance(cached, dict) else {}
        cache[image.image_id] = {
            **previous,
            "event_id": event.event_id,
            "description": event.description,
            "amount": str(amount) if amount is not None else None,
            "lines": [row["text"] for row in lines],
            "hits": [
                {"amount": str(hit.amount), "label": hit.label, "score": hit.score, "raw": hit.raw}
                for hit in sorted(hits, key=lambda item: item.score, reverse=True)[:12]
            ],
        }
        if amount is None:
            unresolved.append(event.event_id)
            continue
        event.amount = amount
        filled.append(
            {
                "event_id": event.event_id,
                "image_id": image.image_id,
                "amount": str(amount),
                "source": source,
            }
        )

    _save_cache(path, cache)

    ocr_by_event = {row["event_id"]: row for row in filled}
    for context in contexts:
        related = [
            ocr_by_event[event.event_id]
            for event in context.events
            if event.event_id in ocr_by_event
        ]
        if related:
            context.facts["ocr"] = related

    return {
        "ocr_filled": len(filled),
        "ocr_unresolved": unresolved,
        "ocr_cache": str(path),
    }


def fill_unresolved_with_gemini(indexes: DataIndexes, contexts: list[RequestContext]) -> dict[str, Any]:
    """Ask Gemini vision only for blank amounts RapidOCR could not read."""
    from llm_gemini import read_image_amount

    path = cache_path(indexes.repo_root)
    cache = _load_cache(path)
    filled: list[dict[str, str]] = []
    still: list[str] = []

    for event in indexes.events_by_id.values():
        if event.amount is not None or event.event_id not in indexes.images_by_event:
            continue
        image = indexes.images_by_event[event.event_id]
        cached = cache.get(image.image_id) or {}
        if cached.get("gemini_amount"):
            event.amount = Decimal(str(cached["gemini_amount"]))
            filled.append({"event_id": event.event_id, "image_id": image.image_id, "amount": str(event.amount)})
            continue
        if not image.exists:
            still.append(event.event_id)
            continue
        raw = read_image_amount(image.path, event.description, indexes.repo_root)
        if not raw:
            still.append(event.event_id)
            continue
        token = re.sub(r"[^\d.]", "", raw)
        try:
            amount = Decimal(token)
        except InvalidOperation:
            still.append(event.event_id)
            continue
        event.amount = amount
        cached = dict(cached)
        cached["gemini_amount"] = str(amount)
        cached["amount"] = str(amount)
        cached["source"] = "gemini"
        cache[image.image_id] = cached
        filled.append({"event_id": event.event_id, "image_id": image.image_id, "amount": str(amount)})

    _save_cache(path, cache)
    for context in contexts:
        extra = [row for row in filled if any(event.event_id == row["event_id"] for event in context.events)]
        if extra:
            context.facts.setdefault("ocr", []).extend(extra)
    return {"gemini_ocr_filled": len(filled), "ocr_unresolved_after_gemini": still}
