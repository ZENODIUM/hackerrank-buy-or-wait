"""Build eligible plans and pick one with the published ranking rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ledger import Flow, apply_spending_changes, legal_changes, simulate
from models import PaymentOption, RequestContext


@dataclass
class Plan:
    method: str
    status: str
    payment_plan: str
    changes: str
    total_paid: Decimal
    start: date | None
    payments: int
    option_id: str
    completes: bool


def money_str(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral():
        return str(int(quantized))
    return f"{quantized:.2f}"


def fmt_plan(pairs: list[tuple[date, Decimal]]) -> str:
    if not pairs:
        return "none"
    return "|".join(f"{day.isoformat()}:{money_str(amount)}" for day, amount in pairs)


def installment_schedule(option: PaymentOption) -> list[tuple[date, Decimal]]:
    day = option.first_payment_date
    rows: list[tuple[date, Decimal]] = []
    for _ in range(option.number_of_payments):
        rows.append((day, option.payment_amount))
        if option.payment_frequency_days:
            day = day + timedelta(days=option.payment_frequency_days)
    return rows


def option_span_months(option: PaymentOption) -> int:
    schedule = installment_schedule(option)
    first = schedule[0][0]
    last = schedule[-1][0]
    return (last.year - first.year) * 12 + last.month - first.month + 1


def _safe(context: RequestContext, flows: list[Flow], extras: list[tuple[date, Decimal]]) -> bool:
    ok, _ = simulate(
        context.profile.current_available_balance,
        context.profile.minimum_balance_to_keep,
        flows,
        extra=[(day, -amount) for day, amount in extras],
        start_day=context.request.request_date,
    )
    return ok


def _change_str(actions: list[str]) -> str:
    return "|".join(actions) if actions else "none"


def collect_plans(
    context: RequestContext, flows: list[Flow], earliest: date | None, safe_today: Decimal
) -> list[Plan]:
    request = context.request
    profile = context.profile
    methods = profile.methods
    deadline = request.desired_completion_date
    plans: list[Plan] = []

    for actions in legal_changes(context, flows):
        adjusted = apply_spending_changes(flows, actions)
        change_label = _change_str(actions)
        used_cuts = bool(actions)

        if "full_payment" in methods and _safe(
            context, adjusted, [(request.request_date, request.requested_amount)]
        ):
            plans.append(
                Plan(
                    method="full_payment",
                    status="affordable_with_plan" if used_cuts else "affordable_now",
                    payment_plan=fmt_plan([(request.request_date, request.requested_amount)]),
                    changes=change_label,
                    total_paid=request.requested_amount,
                    start=request.request_date,
                    payments=1,
                    option_id="",
                    completes=True,
                )
            )

        if (
            request.allows_partial_payment
            and "partial_payment" in methods
            and Decimal(0) < safe_today < request.requested_amount
            and earliest is not None
            and earliest <= deadline
            and _safe(
                context,
                adjusted,
                [
                    (request.request_date, safe_today),
                    (earliest, request.requested_amount - safe_today),
                ],
            )
        ):
            plans.append(
                Plan(
                    method="partial_payment",
                    status="affordable_with_plan",
                    payment_plan=fmt_plan(
                        [
                            (request.request_date, safe_today),
                            (earliest, request.requested_amount - safe_today),
                        ]
                    ),
                    changes=change_label,
                    total_paid=request.requested_amount,
                    start=request.request_date,
                    payments=2,
                    option_id="",
                    completes=True,
                )
            )

        if "installments" in methods and profile.max_installment_months:
            for option in context.options:
                if option.payment_method != "installments":
                    continue
                if option_span_months(option) > profile.max_installment_months:
                    continue
                schedule = installment_schedule(option)
                if schedule[-1][0] > deadline:
                    continue
                if not _safe(context, adjusted, schedule):
                    continue
                plans.append(
                    Plan(
                        method="installments",
                        status="affordable_with_plan",
                        payment_plan=fmt_plan(schedule),
                        changes=change_label,
                        total_paid=option.total_payable_amount,
                        start=schedule[0][0],
                        payments=len(schedule),
                        option_id=option.payment_option_id,
                        completes=True,
                    )
                )

        if (
            "full_payment" in methods
            and earliest is not None
            and earliest > request.request_date
            and earliest <= deadline
            and _safe(context, adjusted, [(earliest, request.requested_amount)])
        ):
            plans.append(
                Plan(
                    method="wait",
                    status="affordable_later",
                    payment_plan=fmt_plan([(earliest, request.requested_amount)]),
                    changes=change_label,
                    total_paid=request.requested_amount,
                    start=earliest,
                    payments=1,
                    option_id="",
                    completes=True,
                )
            )

    if not plans:
        plans.append(
            Plan(
                method="not_recommended",
                status="not_affordable",
                payment_plan="none",
                changes="none",
                total_paid=Decimal("0"),
                start=None,
                payments=0,
                option_id="zzzz",
                completes=False,
            )
        )
    return plans


def rank(plans: list[Plan]) -> Plan:
    def key(plan: Plan):
        n_changes = 0 if plan.changes == "none" else plan.changes.count("|") + 1
        return (
            0 if plan.completes else 1,
            0 if plan.changes == "none" else 1,
            plan.total_paid,
            plan.start or date.max,
            plan.payments,
            n_changes,
            plan.option_id or "zzz",
        )

    return sorted(plans, key=key)[0]


def _ask_clause(context: RequestContext) -> str:
    text = (context.request.request_text or "").strip()
    if not text:
        return context.request.request_type.replace("_", " ")
    cut = text.find(". ")
    first = text[: cut + 1] if 20 < cut < 180 else text
    return first.rstrip()[:220]


def _change_clause(context: RequestContext, plan: Plan) -> str:
    if plan.changes == "none":
        return ""
    parts: list[str] = []
    by_id = {event.event_id: event for event in context.events}
    for action in plan.changes.split("|"):
        bits = action.split(":")
        event = by_id.get(bits[1]) if len(bits) > 1 else None
        label = event.description if event else bits[1]
        if bits[0] == "stop":
            parts.append(f"stop {label}")
        elif bits[0] == "reduce_to" and len(bits) > 2:
            parts.append(f"reduce {label} to {context.profile.home_currency} {bits[2]}")
        else:
            parts.append(action)
    return "Apply " + "; ".join(parts) + ", then "


def _fact_clause(context: RequestContext) -> str:
    facts = context.facts.get("merged") or {}
    bits: list[str] = []
    if facts.get("salary_amount") and (facts.get("salary_date") or facts.get("salary_from")):
        bits.append(
            f"confirmed pay {context.profile.home_currency} {facts['salary_amount']} "
            f"from {facts.get('salary_date') or facts.get('salary_from')}"
        )
    elif facts.get("salary_amount"):
        bits.append(f"confirmed pay {context.profile.home_currency} {facts['salary_amount']}")
    elif facts.get("salary_date") or facts.get("salary_from"):
        bits.append(f"payday moved to {facts.get('salary_date') or facts.get('salary_from')}")
    if facts.get("stop_salary_projection"):
        bits.append("no further salary projected")
    elif facts.get("seasonal_income_continues"):
        bits.append("seasonal/assignment pay still projected after this contract")
    if facts.get("ignore_unconfirmed_credit"):
        bits.append("unconfirmed credits ignored")
    if facts.get("ignore_internal_transfer"):
        bits.append("internal transfers ignored")
    if facts.get("rent_increase_pct"):
        bits.append(f"rent +{facts['rent_increase_pct']}%")
    if facts.get("confirmed_invoice_amount") and facts.get("confirmed_invoice_date"):
        bits.append(
            f"confirmed invoice {context.profile.home_currency} {facts['confirmed_invoice_amount']} "
            f"on {facts['confirmed_invoice_date']}"
        )
    if facts.get("new_recurring_expense_note"):
        bits.append(str(facts["new_recurring_expense_note"]))
    if not bits:
        return ""
    return " Using " + "; ".join(bits) + "."


def explain(context: RequestContext, plan: Plan, safe_today: Decimal) -> str:
    currency = context.profile.home_currency
    floor = money_str(context.profile.minimum_balance_to_keep)
    deadline = context.request.desired_completion_date.isoformat()
    requested = money_str(context.request.requested_amount)
    ask = _ask_clause(context)
    facts = _fact_clause(context)

    if plan.method == "not_recommended":
        if Decimal(0) < safe_today < context.request.requested_amount:
            body = (
                f"Regarding “{ask}”: do not proceed with the {currency} {requested} request. "
                f"Although {currency} {money_str(safe_today)} is available today, "
                "the full amount cannot be completed safely within 90 days."
            )
        else:
            body = (
                f"Regarding “{ask}”: do not make this payment by {deadline}. "
                f"None of the available options keeps the {currency} {floor} minimum protected."
            )
        return body + facts
    if plan.method == "wait" and plan.start is not None:
        return (
            f"Regarding “{ask}”: pay {currency} {requested} in full on {plan.start.isoformat()}. "
            f"Paying earlier would take the balance below the {currency} {floor} minimum."
            + facts
        )
    if plan.method == "partial_payment":
        rest = context.request.requested_amount - safe_today
        return (
            f"Regarding “{ask}”: pay {currency} {money_str(safe_today)} today and the remaining "
            f"{currency} {money_str(rest)} later. "
            f"This completes the full request and keeps the {currency} {floor} minimum protected."
            + facts
        )
    if plan.method == "installments":
        first = plan.payment_plan.split("|", 1)[0]
        amount = first.split(":", 1)[1] if ":" in first else requested
        return (
            f"Regarding “{ask}”: use {plan.payments} installments of {currency} {amount}, "
            f"starting {plan.start}. This leaves at least {currency} {floor} available."
            + facts
        )
    prefix = _change_clause(context, plan)
    return (
        f"Regarding “{ask}”: {prefix}pay {currency} {requested} today. "
        f"This leaves at least {currency} {floor} available over the next 90 days."
        + facts
    )


def decide_context(context: RequestContext, ledger: dict[str, Any]) -> dict[str, Any]:
    flows: list[Flow] = ledger["flows"]
    safe: Decimal = ledger["amount_safe_to_pay"]
    earliest = ledger["earliest"]
    chosen = rank(collect_plans(context, flows, earliest, safe))
    earliest_out = earliest.isoformat() if earliest is not None else ""
    if chosen.status == "affordable_now":
        earliest_out = context.request.request_date.isoformat()
    row = {
        "request_id": context.request.request_id,
        "amount_safe_to_pay": money_str(safe),
        "affordability_status": chosen.status,
        "recommended_payment_method": chosen.method,
        "payment_plan": chosen.payment_plan,
        "earliest_date_for_full_payment": earliest_out,
        "spending_changes_needed": chosen.changes,
        "decision_explanation": explain(context, chosen, safe),
    }
    context.facts["decision"] = row
    return row
