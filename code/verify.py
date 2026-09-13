"""Contract checks, root output.csv, and evaluation/usage_report.md."""

from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from decide import fmt_plan, installment_schedule
from llm_gemini import last_live_usage, usage_snapshot
from models import DataIndexes, RequestContext

OUTPUT_COLUMNS = [
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
]

STATUSES = {"affordable_now", "affordable_with_plan", "affordable_later", "not_affordable"}
METHODS = {"full_payment", "partial_payment", "installments", "wait", "not_recommended"}


def _decimal(value: str) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def check_row(context: RequestContext, row: dict[str, str]) -> list[str]:
    errors: list[str] = []
    request = context.request
    rid = request.request_id
    safe = _decimal(row.get("amount_safe_to_pay", ""))
    if safe is None or safe < 0 or safe > request.requested_amount:
        errors.append(f"{rid}: amount_safe_to_pay out of bounds")
    if row.get("affordability_status") not in STATUSES:
        errors.append(f"{rid}: bad affordability_status")
    if row.get("recommended_payment_method") not in METHODS:
        errors.append(f"{rid}: bad recommended_payment_method")

    method = row.get("recommended_payment_method")
    status = row.get("affordability_status")
    plan = row.get("payment_plan") or ""
    if method == "not_recommended":
        if status != "not_affordable" or plan != "none":
            errors.append(f"{rid}: not_recommended contract")
    if method == "wait" and status != "affordable_later":
        errors.append(f"{rid}: wait must be affordable_later")
    if method == "partial_payment":
        if status != "affordable_with_plan":
            errors.append(f"{rid}: partial must be affordable_with_plan")
        parts = plan.split("|")
        if len(parts) != 2:
            errors.append(f"{rid}: partial must have exactly two payments")
        elif safe is not None:
            first_day, first_amt = parts[0].split(":", 1)
            second_day, second_amt = parts[1].split(":", 1)
            if first_day != request.request_date.isoformat():
                errors.append(f"{rid}: partial first date")
            if _decimal(first_amt) != safe:
                errors.append(f"{rid}: partial first amount")
            if _decimal(first_amt) + _decimal(second_amt) != request.requested_amount:
                errors.append(f"{rid}: partial amounts must sum to requested")
            if second_day != row.get("earliest_date_for_full_payment"):
                errors.append(f"{rid}: partial second date must be earliest")
    if method == "installments":
        matched = False
        for option in context.options:
            if option.payment_method != "installments":
                continue
            if fmt_plan(installment_schedule(option)) == plan:
                matched = True
                break
        if not matched:
            errors.append(f"{rid}: installment plan does not match a supplied option")
    if status == "affordable_now" and row.get("earliest_date_for_full_payment") != request.request_date.isoformat():
        errors.append(f"{rid}: affordable_now earliest must be request_date")
    return errors


def output_path_for_mode(repo: Path, mode: str) -> Path:
    if mode == "eval":
        return repo / "output.csv"
    path = repo / "code" / "data_layer" / f"{mode}_output.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_output(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in OUTPUT_COLUMNS})


def _token_cost(inp: int, out: int) -> float:
    """Gemini 3.5 Flash-Lite paid list price. Interval/throttle does not change this."""
    return inp / 1_000_000 * 0.10 + out / 1_000_000 * 0.40


def write_usage_report(path: Path, request_count: int, extra: dict[str, Any] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    usage = usage_snapshot()
    calls = int(usage.get("calls") or 0)
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    prior = last_live_usage()
    extras = extra or {}
    mode = str(extras.get("mode") or "")
    denom = max(request_count, 1)

    # Submission totals: live Gemini work that the predictions depend on.
    # A cache-only rerun has 0 this-process tokens; graders still need the
    # extract that wrote facts.json / ocr.json for this output.csv.
    if calls > 0:
        live_model = str(usage.get("model") or "gemini-3.5-flash-lite")
        live_calls, live_in, live_out = calls, inp, out
        call_note = "this process (live)"
    elif prior:
        live_model = str(prior.get("model") or "gemini-3.5-flash-lite")
        live_calls = int(prior.get("calls") or 0)
        live_in = int(prior.get("input_tokens") or 0)
        live_out = int(prior.get("output_tokens") or 0)
        call_note = "cache reuse of the live extract below"
    else:
        live_model = "gemini-3.5-flash-lite"
        live_calls = live_in = live_out = 0
        call_note = "none recorded"
    live_total = live_in + live_out
    live_cost = _token_cost(live_in, live_out)
    heading = (
        "Final full-dataset run that produced root `output.csv`."
        if mode == "eval"
        else f"This file was written by `--mode {mode or 'unknown'}`. It is not the final 250-row eval report."
    )
    lines = [
        "# Token usage report",
        "",
        heading,
        "",
        "## Scope",
        "",
        "- These counts are **Google Gemini tokens used by this Buy or Wait pipeline** (message facts and, if needed, vision on unread bills).",
        "- They do **not** include Cursor, IDE, or chat-agent tokens.",
        "- Ranking, the 90-day cash walk, and `amount_safe_to_pay` are deterministic Python (0 tokens).",
        "",
        "## Models",
        "",
        "- Provider: google",
        f"- Model: `{live_model}`",
        "- Models used: 1 (no second model)",
        f"- This-process Gemini: {call_note}",
        "- Role: Gemini reads every uncached message; Gemini vision only if RapidOCR leaves an amount blank",
        "",
        "## Totals (Gemini workflow that produced these predictions)",
        "",
        f"- Model calls: {live_calls}",
        f"- Input tokens: {live_in}",
        f"- Output tokens: {live_out}",
        f"- Total tokens: {live_total}",
        f"- Requests scored: {request_count}",
        f"- Average tokens per request: {live_total / denom:.4f}",
        f"- Estimated cost: ${live_cost:.4f} (Gemini Flash-Lite list: $0.10/1M in, $0.40/1M out)",
        f"- Estimated cost per request: ${live_cost / denom:.6f}",
        "",
        "## This process (the eval that wrote output.csv)",
        "",
        f"- Model calls: {calls}",
        f"- Input tokens: {inp}",
        f"- Output tokens: {out}",
        f"- Total tokens: {inp + out}",
        "",
        "## Notes",
        "",
        "- API key is read from `GEMINI_API_KEY` or `GOOGLE_API_KEY` (never written to this file or to git).",
        "- Cached message facts and OCR amounts make a later eval show 0 new calls. That is reuse of the live extract, not an outage.",
        "- There is no regex fallback. Uncached messages call Gemini; cached ones apply stored JSON facts.",
        "- RapidOCR handles printed bills; Gemini vision is used only when OCR cannot read an amount.",
    ]
    fail_count = int(usage.get("failures") or 0)
    extra_fails = int((extras or {}).get("message_llm_failures") or 0)
    lines.extend(
        [
            "",
            "## Hard failures",
            "",
            f"- Gemini call/parse failures this process: {fail_count}",
            f"- Messages that returned no JSON this process: {extra_fails}",
            "- A hard failure is not cached as a usable fact. The next run retries that message.",
            "- A successful `{}` (nothing usable in the text) is a call, not a failure.",
        ]
    )
    if usage.get("errors"):
        lines.extend(["", "## Errors (redacted)", ""])
        for item in usage["errors"][:8]:
            lines.append(f"- `{item}`")
        leftover = len(usage["errors"]) - 8
        if leftover > 0:
            lines.append(f"- … {leftover} more")
    if extras:
        lines.extend(["", "## Run stats", ""])
        for key, value in extras.items():
            lines.append(f"- {key}: {value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_alignment(path: Path, score: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Sample alignment",
        "",
        "Predictions used only the input columns of `sample_requests.csv`. Gold columns were not read until this compare step.",
        "",
        f"- Requests: {score['n']}",
        f"- Field matches: {score['matches']}",
        "",
        "| request_id | amount | status | method | plan | earliest | changes |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in score.get("comparison") or []:
        cells = [row["request_id"]]
        for field in (
            "amount_safe_to_pay",
            "affordability_status",
            "recommended_payment_method",
            "payment_plan",
            "earliest_date_for_full_payment",
            "spending_changes_needed",
        ):
            mark = "Y" if row.get(f"ok_{field}") == "Y" else "N"
            cells.append(mark)
        lines.append("| " + " | ".join(cells) + " |")
    if score.get("mismatches"):
        lines.extend(["", "## Mismatches", ""])
        for item in score["mismatches"]:
            lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def score_samples(rows: list[dict[str, str]], gold_path: Path) -> dict[str, Any]:
    with gold_path.open(encoding="utf-8", newline="") as handle:
        gold = {row["request_id"]: row for row in csv.DictReader(handle)}
    fields = [
        "amount_safe_to_pay",
        "affordability_status",
        "recommended_payment_method",
        "payment_plan",
        "earliest_date_for_full_payment",
        "spending_changes_needed",
    ]
    tallies = {field: 0 for field in fields}
    mismatches: list[str] = []
    comparison_rows: list[dict[str, str]] = []
    for row in rows:
        expected = gold.get(row["request_id"])
        if not expected:
            continue
        entry = {"request_id": row["request_id"]}
        for field in fields:
            pred = (row.get(field) or "").strip()
            exp = (expected.get(field) or "").strip()
            if field == "amount_safe_to_pay":
                left, right = _decimal(pred), _decimal(exp)
                ok = left is not None and right is not None and left == right
            else:
                ok = pred == exp
            entry[f"pred_{field}"] = pred
            entry[f"gold_{field}"] = exp
            entry[f"ok_{field}"] = "Y" if ok else "N"
            if ok:
                tallies[field] += 1
            else:
                mismatches.append(f"{row['request_id']} {field}: got {pred!r} expected {exp!r}")
        comparison_rows.append(entry)
    return {"n": len(rows), "matches": tallies, "mismatches": mismatches, "comparison": comparison_rows}


def verify_and_write(
    indexes: DataIndexes,
    contexts: list[RequestContext],
    rows: list[dict[str, str]],
    mode: str,
    run_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    by_id = {context.request.request_id: context for context in contexts}
    for row in rows:
        context = by_id.get(row["request_id"])
        if context is None:
            errors.append(f"missing context for {row['request_id']}")
            continue
        errors.extend(check_row(context, row))

    repo = indexes.repo_root
    out_path = output_path_for_mode(repo, mode)
    write_output(out_path, rows)

    report_paths = [
        repo / "code" / "evaluation" / "usage_report.md",
        repo / "evaluation" / "usage_report.md",
    ]
    extra = {
        "mode": mode,
        "output": "output.csv" if mode == "eval" else out_path.name,
        "contract_errors": len(errors),
        "message_llm_failures": int((run_stats or {}).get("message_llm_failures") or 0),
        "message_llm_calls": int((run_stats or {}).get("message_llm_calls") or 0),
        "message_cache_hits": int((run_stats or {}).get("message_cache_hits") or 0),
    }
    sample_score = None
    if mode == "samples":
        sample_score = score_samples(rows, repo / "dataset" / "sample_requests.csv")
        extra["sample_matches"] = sample_score["matches"]
        _write_alignment(repo / "code" / "data_layer" / "sample_alignment.md", sample_score)
    for path in report_paths:
        write_usage_report(path, len(rows), extra)

    return {
        "output_csv": str(out_path),
        "contract_errors": errors[:20],
        "contract_error_count": len(errors),
        "usage": usage_snapshot(),
        "sample_score": sample_score,
        "usage_report": str(report_paths[0]),
    }
