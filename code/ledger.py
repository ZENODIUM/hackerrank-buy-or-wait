"""Deterministic 90-day cash forecast."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from models import DataIndexes, Event, Profile, Request, RequestContext

FORECAST_DAYS = 90


@dataclass(slots=True)
class Flow:
    on: date
    amount: Decimal  # signed: +credit / -debit
    kind: str
    event_id: str | None = None
    category: str = ""
    flexible: str = "fixed"
    min_amount: Decimal | None = None


def add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    last = (date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def fx_convert(indexes: DataIndexes, amount: Decimal, src: str, dst: str, on: date) -> Decimal:
    if src == dst:
        return amount
    exact = indexes.fx.get((on, src, dst))
    if exact is not None:
        return (amount * exact).quantize(Decimal("0.01"))
    exact_inv = indexes.fx.get((on, dst, src))
    if exact_inv is not None:
        return (amount / exact_inv).quantize(Decimal("0.01"))
    candidates = [
        (day, rate)
        for (day, frm, to), rate in indexes.fx.items()
        if frm == src and to == dst and day <= on
    ]
    if candidates:
        return (amount * max(candidates)[1]).quantize(Decimal("0.01"))
    inverse = [
        (day, rate)
        for (day, frm, to), rate in indexes.fx.items()
        if frm == dst and to == src and day <= on
    ]
    if inverse:
        return (amount / max(inverse)[1]).quantize(Decimal("0.01"))
    return amount


def _cash_day(event: Event) -> date:
    return event.settlement_date or event.event_date


def _keep_event(event: Event, facts: dict[str, Any]) -> bool:
    if event.status in {"cancelled", "failed"}:
        return False
    if event.direction == "non_cash" or event.status == "unrealized":
        return False
    if event.amount is None:
        return False
    if event.status == "pending" and event.direction == "credit":
        return False
    if (
        facts.get("ignore_unconfirmed_credit")
        and event.direction == "credit"
        and event.status != "settled"
        and event.category != "salary"
    ):
        return False
    if facts.get("unrealized_only") and event.event_type.startswith("investment"):
        return False
    return True


def _superseded_ids(events: list[Event], today: date) -> set[str]:
    """Drop a linked row that is the same cash hit twice.

    The six 'Possible duplicate card charge' rows link a later pending
    debit to the settled original (same category, direction, amount).
    Pending debits are otherwise reserved, so both would hit the walk.
    Conflict rules: settled over pending; keep the earlier original.
    Opposite-direction links (refund / reimbursement) are not duplicates.
    """
    by_id = {event.event_id: event for event in events}
    drop: set[str] = set()
    rank = {"settled": 3, "scheduled": 2, "pending": 1, "unrealized": 0, "failed": 0, "cancelled": 0}
    for event in events:
        other_id = event.linked_event_id
        if not other_id or other_id not in by_id:
            continue
        other = by_id[other_id]
        if event.category != other.category or event.direction != other.direction:
            continue
        if event.amount is None or other.amount is None or event.amount != other.amount:
            continue
        gap = abs((_cash_day(event) - _cash_day(other)).days)
        if gap > 21:
            continue
        event_rank = rank.get(event.status, 0)
        other_rank = rank.get(other.status, 0)
        if event_rank != other_rank:
            drop.add(event.event_id if event_rank < other_rank else other.event_id)
            continue
        later = event if _cash_day(event) > _cash_day(other) else other
        drop.add(later.event_id)
    return drop


def _explicit_flows(context: RequestContext, indexes: DataIndexes) -> list[Flow]:
    today = context.request.request_date
    home = context.profile.home_currency
    facts = context.facts.get("merged") or {}
    skip = _superseded_ids(context.events, today)
    flows: list[Flow] = []
    for event in context.events:
        if event.event_id in skip:
            continue
        if not _keep_event(event, facts):
            continue
        day = _cash_day(event)
        if day < today:
            continue
        amount = fx_convert(indexes, event.amount or Decimal(0), event.currency, home, day)
        signed = amount if event.direction == "credit" else -amount
        if facts.get("ignore_internal_transfer") and event.category in {"transfer", "internal"}:
            continue
        flows.append(
            Flow(
                on=day,
                amount=signed,
                kind="explicit",
                event_id=event.event_id,
                category=event.category,
                flexible=event.flexibility,
                min_amount=event.minimum_allowed_amount,
            )
        )
    return flows


_ONE_OFF_WORDS = (
    "arrears",
    "prorated",
    "bonus",
    "commission",
    "one-time",
    "one-off",
    "reversal",
    "refund",
    "prize",
    "lottery",
    "tunggakan",
    "penyesuaian satu kali",
)
_FINAL_PAY_WORDS = ("final employer", "final payroll", "last payroll", "final salary")
_SKIP_PROJECT_CATEGORIES = {"shopping", "windfall"}
_SKIP_PROJECT_TYPES = {"refund"}
_VARIABLE_CATEGORIES = {"groceries", "transport", "dining", "entertainment"}
_FREELANCE_WORDS = (
    "invoice",
    "project",
    "client",
    "retainer",
    "freelance",
    "contract",
    "independent",
    "milestone",
    "website",
    "faktur",
)


def _is_one_off(event: Event) -> bool:
    blob = f"{event.description} {event.event_type}".lower()
    return any(word in blob for word in _ONE_OFF_WORDS)


def _looks_freelance(event: Event) -> bool:
    blob = f"{event.description} {event.event_type} {event.category}".lower()
    return any(word in blob for word in _FREELANCE_WORDS)


def _series_lapsed(last_day: date, today: date, typical_days: int) -> bool:
    """Stop projecting a series that missed its last expected cycle.

    If the last hit is already on/after today (scheduled or explicit next),
    the series is still live. Otherwise a gap > 1.5× its own cadence means
    it lapsed — e.g. four monthly paydays then a skipped month.
    """
    if last_day >= today or typical_days <= 0:
        return False
    return (today - last_day).days * 2 > typical_days * 3


def _median_days(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _typical_amount(indexes: DataIndexes, items: list[Event], home: str) -> Decimal:
    pool = [event for event in items if event.amount is not None and not _is_one_off(event)]
    if len(pool) < 3:
        pool = [event for event in items if event.amount is not None]
    amounts = [
        fx_convert(indexes, event.amount or Decimal(0), event.currency, home, _cash_day(event))
        for event in pool[-8:]
    ]
    if not amounts:
        return Decimal(0)
    ordered = sorted(amounts)
    mid = ordered[len(ordered) // 2]
    cleaned = [amt for amt in ordered if amt <= mid * Decimal("2.5")] or ordered
    return cleaned[len(cleaned) // 2]


def _day_clusters(items: list[Event]) -> list[list[Event]]:
    regular = [event for event in items if not _is_one_off(event)]
    pool = sorted(regular if len(regular) >= 2 else items, key=_cash_day)
    if len(pool) >= 2:
        raw = [(_cash_day(pool[i]) - _cash_day(pool[i - 1])).days for i in range(1, len(pool))]
        mixed = _median_days([delta for delta in raw if delta > 0] or raw)
        if mixed <= 12:
            return [_regular_series(items)]
    by_day: dict[int, list[Event]] = defaultdict(list)
    for event in pool:
        by_day[event.event_date.day].append(event)
    monthly: list[list[Event]] = []
    for group in by_day.values():
        if len(group) < 3:
            continue
        ordered = sorted(group, key=_cash_day)
        deltas = [(_cash_day(ordered[i]) - _cash_day(ordered[i - 1])).days for i in range(1, len(ordered))]
        typical = _median_days([delta for delta in deltas if delta > 0] or deltas)
        if 25 <= typical <= 35:
            monthly.append(ordered)
    # Freelance retainers often land on two fixed month-days (7th and 20th).
    # Ignore accidental day-of-month collisions from weekly gig payouts.
    if len(monthly) >= 2:
        return monthly
    return [_regular_series(items)]


def _regular_series(items: list[Event]) -> list[Event]:
    regular = [event for event in items if not _is_one_off(event)]
    pool = sorted(regular if len(regular) >= 2 else items, key=_cash_day)
    if len(pool) < 2:
        return pool
    raw = [(_cash_day(pool[i]) - _cash_day(pool[i - 1])).days for i in range(1, len(pool))]
    typical = _median_days([delta for delta in raw if delta > 0] or raw)
    # Day-of-month clustering is for monthly bills/salary only. Weekly
    # groceries share a category but not a calendar day.
    if not (25 <= typical <= 35):
        return pool
    by_day: dict[int, list[Event]] = defaultdict(list)
    for event in pool:
        by_day[event.event_date.day].append(event)
    best_day = max(by_day, key=lambda day: (len(by_day[day]), -abs(day - 15)))
    if len(by_day[best_day]) >= 2:
        return sorted(by_day[best_day], key=_cash_day)
    return pool


def _project_series(context: RequestContext, indexes: DataIndexes) -> list[Flow]:
    today = context.request.request_date
    horizon = today + timedelta(days=FORECAST_DAYS)
    home = context.profile.home_currency
    facts = context.facts.get("merged") or {}
    series_events = [
        event
        for event in context.events
        if event.amount is not None
        and event.direction not in {"non_cash"}
        and event.status in {"settled", "scheduled"}
    ]
    groups: dict[tuple[str, str], list[Event]] = defaultdict(list)
    for event in series_events:
        if event.category in _SKIP_PROJECT_CATEGORIES or event.event_type in _SKIP_PROJECT_TYPES:
            continue
        groups[(event.category, event.direction)].append(event)

    flows: list[Flow] = []
    for (category, direction), items in groups.items():
        if category == "salary" and (
            facts.get("stop_salary_projection")
            or any(any(word in event.description.lower() for word in _FINAL_PAY_WORDS) for event in items)
            or (
                facts.get("confirmed_invoice_amount")
                and items
                and all(_looks_freelance(event) for event in items)
            )
        ):
            continue
        series_list = (
            _day_clusters(items)
            if category == "salary" and not facts.get("ignore_unconfirmed_credit")
            else [_regular_series(items)]
        )
        for series in series_list:
            if len(series) < 2:
                continue
            deltas = [(_cash_day(series[i]) - _cash_day(series[i - 1])).days for i in range(1, len(series))]
            typical_days = _median_days([delta for delta in deltas if delta > 0] or deltas)
            last = series[-1]
            last_day = _cash_day(last)
            amount = fx_convert(indexes, last.amount or Decimal(0), last.currency, home, last_day)
            if category == "rent" and facts.get("rent_increase_pct"):
                amount = (amount * (Decimal(1) + Decimal(str(facts["rent_increase_pct"])) / Decimal(100))).quantize(
                    Decimal("0.01")
                )
            if category in _VARIABLE_CATEGORIES or (
                category == "salary" and facts.get("seasonal_income_continues")
            ):
                amount = _typical_amount(indexes, series, home)

            salary_fact = None
            salary_fact_from: date | None = None
            if category == "salary" and facts.get("salary_amount"):
                fact_amt = Decimal(str(facts["salary_amount"]))
                for key in ("salary_date", "salary_from"):
                    raw = facts.get(key)
                    if not raw:
                        continue
                    try:
                        parsed = date.fromisoformat(str(raw))
                    except ValueError:
                        parsed = None
                    if parsed is not None:
                        salary_fact_from = parsed
                # Safer reading: an undated higher figure next to
                # "commission not approved" is not confirmed cash.
                if facts.get("ignore_unconfirmed_credit") and fact_amt > amount:
                    salary_fact = None
                else:
                    salary_fact = fact_amt

            step_days = 0
            use_months = False
            next_only = False
            if 25 <= typical_days <= 35:
                use_months = True
            elif 5 <= typical_days <= 10 and category in {"groceries", "transport"} and len(series) >= 3:
                step_days = typical_days
            elif (
                18 <= typical_days <= 24
                and category in {"dining", "entertainment", "groceries", "transport"}
                and (category in {"dining", "entertainment"} or len(series) >= 3)
            ):
                step_days = typical_days
            elif 11 <= typical_days <= 17 and category in {"groceries", "transport"} and len(series) >= 3:
                step_days = typical_days
            elif 11 <= typical_days <= 17 and category in {"dining", "entertainment"}:
                step_days = typical_days
                next_only = True
            elif 5 <= typical_days <= 10 and category == "dining":
                # One upcoming meal is expected; 90 days of weekly dining is not.
                step_days = typical_days
                next_only = True
            elif 5 <= typical_days <= 24 and direction == "credit" and category == "salary":
                if facts.get("ignore_unconfirmed_credit"):
                    continue
                step_days = typical_days

            if _series_lapsed(last_day, today, typical_days):
                # Do not loosen the 1.5× gap rule. Seasonal earners with a
                # "this contract ended, another may be approved" message are
                # still active; only that salary series is exempt.
                if not (category == "salary" and facts.get("seasonal_income_continues")):
                    continue

            def _emit(on: date, pay: Decimal) -> None:
                signed = pay if direction == "credit" else -pay
                flows.append(
                    Flow(
                        on=on,
                        amount=signed,
                        kind="recurring",
                        event_id=last.event_id,
                        category=category,
                        flexible=last.flexibility,
                        min_amount=last.minimum_allowed_amount,
                    )
                )

            def _salary_pay(on: date, first: bool) -> Decimal:
                if salary_fact is None:
                    return amount
                if salary_fact_from is not None and salary_fact_from <= on:
                    return salary_fact
                if salary_fact_from is None:
                    return salary_fact
                return amount

            if use_months:
                cursor = add_months(last_day, 1)
                if category == "salary" and salary_fact_from is not None and salary_fact_from >= today:
                    cursor = salary_fact_from
                first = True
                while cursor <= horizon:
                    if cursor >= today:
                        _emit(cursor, _salary_pay(cursor, first))
                        first = False
                    cursor = add_months(cursor, 1)
            elif step_days:
                cursor = last_day + timedelta(days=step_days)
                first = True
                while cursor <= horizon:
                    if cursor >= today:
                        _emit(cursor, _salary_pay(cursor, first))
                        first = False
                        if next_only:
                            break
                    cursor += timedelta(days=step_days)

    salary_amt = facts.get("salary_amount")
    salary_on = facts.get("salary_date") or facts.get("salary_from")
    if salary_amt and salary_on and not facts.get("stop_salary_projection"):
        try:
            salary_start = date.fromisoformat(str(salary_on)[:10])
        except ValueError:
            salary_start = None
        if salary_start is not None and salary_start >= today:
            already = any(item.category == "salary" and item.amount > 0 for item in flows)
            if not already:
                pay = Decimal(str(salary_amt))
                cursor = salary_start
                while cursor <= horizon:
                    flows.append(
                        Flow(
                            on=cursor,
                            amount=pay,
                            kind="recurring",
                            category="salary",
                        )
                    )
                    cursor = add_months(cursor, 1)
    if facts.get("one_off_credit"):
        pay_day = facts.get("salary_date") or facts.get("salary_from")
        if pay_day:
            flows.append(
                Flow(
                    on=date.fromisoformat(str(pay_day)),
                    amount=Decimal(str(facts["one_off_credit"])),
                    kind="one_off",
                    category="salary",
                )
            )
    note = str(facts.get("new_recurring_expense_note") or "").lower()
    if "child" in note:
        child_events = [
            event
            for event in context.events
            if event.amount is not None
            and event.direction == "debit"
            and (
                "child" in event.category.lower()
                or "child" in event.description.lower()
                or "dependen" in event.description.lower()
            )
        ]
        if child_events:
            last = max(child_events, key=_cash_day)
            last_day = _cash_day(last)
            pay = fx_convert(indexes, last.amount or Decimal(0), last.currency, home, last_day)
            cursor = add_months(last_day, 1)
            while cursor <= horizon:
                if cursor >= today:
                    flows.append(
                        Flow(
                            on=cursor,
                            amount=-pay,
                            kind="recurring",
                            event_id=last.event_id,
                            category=last.category,
                            flexible=last.flexibility,
                            min_amount=last.minimum_allowed_amount,
                        )
                    )
                cursor = add_months(cursor, 1)
    if facts.get("confirmed_invoice_amount") and facts.get("confirmed_invoice_date"):
        flows.append(
            Flow(
                on=date.fromisoformat(str(facts["confirmed_invoice_date"])),
                amount=Decimal(str(facts["confirmed_invoice_amount"])),
                kind="one_off",
                category="invoice",
            )
        )
    return flows


def merge_flows(explicit: list[Flow], projected: list[Flow]) -> list[Flow]:
    covered = {(item.on, item.category, item.amount > 0) for item in explicit}
    merged = list(explicit)
    for item in projected:
        key = (item.on, item.category, item.amount > 0)
        if key in covered:
            continue
        merged.append(item)
    return sorted(merged, key=lambda item: (item.on, item.event_id or ""))


def simulate(
    start_balance: Decimal,
    floor: Decimal,
    flows: list[Flow],
    extra: list[tuple[date, Decimal]] | None = None,
    start_day: date | None = None,
    horizon_days: int = FORECAST_DAYS,
) -> tuple[bool, Decimal]:
    extras = extra or []
    days: dict[date, Decimal] = {}
    for item in flows:
        days[item.on] = days.get(item.on, Decimal(0)) + item.amount
    for on, amount in extras:
        days[on] = days.get(on, Decimal(0)) + amount
    if start_day is None:
        start_day = min(days) if days else date.today()
    end = start_day + timedelta(days=horizon_days)
    balance = start_balance
    trough = start_balance
    day = start_day
    while day <= end:
        balance += days.get(day, Decimal(0))
        if balance < trough:
            trough = balance
        if balance < floor:
            return False, trough
        day += timedelta(days=1)
    return True, trough


def amount_safe_today(context: RequestContext, flows: list[Flow]) -> Decimal:
    profile = context.profile
    request = context.request
    ok_full, _ = simulate(
        profile.current_available_balance,
        profile.minimum_balance_to_keep,
        flows,
        extra=[(request.request_date, -request.requested_amount)],
        start_day=request.request_date,
    )
    if ok_full:
        return request.requested_amount
    low = Decimal("0")
    high = request.requested_amount
    best = Decimal("0")
    while high - low > Decimal("0.01"):
        mid = ((low + high) / 2).quantize(Decimal("0.01"))
        ok, _ = simulate(
            profile.current_available_balance,
            profile.minimum_balance_to_keep,
            flows,
            extra=[(request.request_date, -mid)],
            start_day=request.request_date,
        )
        if ok:
            best = mid
            low = mid
        else:
            high = mid - Decimal("0.01")
    return min(best, request.requested_amount)


def earliest_full(context: RequestContext, flows: list[Flow]) -> date | None:
    request = context.request
    profile = context.profile
    start = request.request_date
    end = start + timedelta(days=FORECAST_DAYS)
    cursor = start
    while cursor <= end:
        ok, _ = simulate(
            profile.current_available_balance,
            profile.minimum_balance_to_keep,
            flows,
            extra=[(cursor, -request.requested_amount)],
            start_day=start,
        )
        if ok:
            return cursor
        cursor += timedelta(days=1)
    return None


def apply_spending_changes(flows: list[Flow], actions: list[str]) -> list[Flow]:
    updated: list[Flow] = []
    for item in flows:
        skip = False
        new_amount = item.amount
        for action in actions:
            if action.startswith("stop:") and item.event_id == action.split(":", 1)[1] and item.amount < 0:
                skip = True
            if action.startswith("reduce_to:") and item.event_id:
                _tag, event_id, raw = action.split(":", 2)
                if item.event_id == event_id and item.amount < 0:
                    new_amount = -Decimal(raw)
        if not skip:
            updated.append(
                Flow(
                    on=item.on,
                    amount=new_amount,
                    kind=item.kind,
                    event_id=item.event_id,
                    category=item.category,
                    flexible=item.flexible,
                    min_amount=item.min_amount,
                )
            )
    return updated


def legal_changes(context: RequestContext, flows: list[Flow]) -> list[list[str]]:
    profile = context.profile
    options: list[list[str]] = [[]]
    seen: set[str] = set()
    for item in flows:
        if not item.event_id or item.event_id in seen or item.amount >= 0:
            continue
        seen.add(item.event_id)
        if item.category in profile.protect:
            continue
        if item.flexible in {"stoppable", "reducible_or_stoppable"} and item.category in profile.can_stop:
            options.append([f"stop:{item.event_id}"])
        if (
            item.flexible in {"reducible", "reducible_or_stoppable"}
            and item.category in profile.can_reduce
            and item.min_amount is not None
        ):
            floor = item.min_amount.quantize(Decimal("0.01"))
            floor_text = str(int(floor)) if floor == floor.to_integral() else f"{floor:.2f}"
            options.append([f"reduce_to:{item.event_id}:{floor_text}"])
    # keep empty + up to a few singles; combine at most two different events
    singles = [item for item in options if len(item) == 1][:4]
    combos = [[]]
    combos.extend(singles)
    for i, left in enumerate(singles):
        for right in singles[i + 1 :]:
            if left[0].split(":")[1] != right[0].split(":")[1]:
                combos.append(left + right)
    return combos[:8]


def build_ledger(context: RequestContext, indexes: DataIndexes) -> dict[str, Any]:
    explicit = _explicit_flows(context, indexes)
    projected = _project_series(context, indexes)
    flows = merge_flows(explicit, projected)
    safe = amount_safe_today(context, flows)
    earliest = earliest_full(context, flows)
    ok_none, trough = simulate(
        context.profile.current_available_balance,
        context.profile.minimum_balance_to_keep,
        flows,
        start_day=context.request.request_date,
    )
    context.facts["ledger"] = {
        "flow_count": len(flows),
        "amount_safe_to_pay": str(safe),
        "earliest_date_for_full_payment": earliest.isoformat() if earliest else "",
        "baseline_safe": ok_none,
        "trough": str(trough),
    }
    return {"flows": flows, "amount_safe_to_pay": safe, "earliest": earliest, "trough": trough}
