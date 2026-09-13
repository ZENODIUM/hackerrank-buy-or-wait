"""Contract-check output.csv and show the Gemini usage report.

Does not read gold labels. Run from the repo root:

    python code/evaluation/main.py
"""

from __future__ import annotations

import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

COLUMNS = [
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


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        return None


def check_output(repo: Path) -> list[str]:
    out_path = repo / "output.csv"
    req_path = repo / "dataset" / "requests.csv"
    errors: list[str] = []
    if not out_path.is_file():
        return [f"missing {out_path}"]
    if not req_path.is_file():
        return [f"missing {req_path}"]
    with out_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    with req_path.open(encoding="utf-8", newline="") as handle:
        requests = list(csv.DictReader(handle))
    if list(rows[0].keys())[:8] != COLUMNS if rows else True:
        header = list(rows[0].keys()) if rows else []
        if header != COLUMNS:
            errors.append(f"column order {header} != {COLUMNS}")
    req_ids = [item["request_id"] for item in requests]
    out_ids = [item["request_id"] for item in rows]
    if out_ids != req_ids:
        errors.append(f"id mismatch: {len(out_ids)} output vs {len(req_ids)} requests")
    by_req = {item["request_id"]: item for item in requests}
    for row in rows:
        rid = row.get("request_id", "")
        req = by_req.get(rid)
        if req is None:
            errors.append(f"{rid}: not in requests.csv")
            continue
        safe = _decimal(row.get("amount_safe_to_pay", ""))
        asked = _decimal(req.get("requested_amount", ""))
        if safe is None or asked is None or safe < 0 or safe > asked:
            errors.append(f"{rid}: amount_safe_to_pay out of bounds")
        if row.get("affordability_status") not in STATUSES:
            errors.append(f"{rid}: bad status")
        if row.get("recommended_payment_method") not in METHODS:
            errors.append(f"{rid}: bad method")
        if not (row.get("decision_explanation") or "").strip():
            errors.append(f"{rid}: empty explanation")
    return errors


def main() -> int:
    repo = _root()
    errors = check_output(repo)
    report = repo / "evaluation" / "usage_report.md"
    if not report.is_file():
        report = repo / "code" / "evaluation" / "usage_report.md"
    print(f"repo: {repo}")
    print(f"contract_errors: {len(errors)}")
    for item in errors[:20]:
        print(f"  {item}")
    print()
    if report.is_file():
        print(report.read_text(encoding="utf-8"))
    else:
        print("usage_report.md not found. Run: python code/main.py --mode eval")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
