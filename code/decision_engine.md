# Decision engine — input to output

This is the rulebook. Python owns every scored column. Gemini never picks `full_payment` vs `wait`. It only reads a message or a handwritten image amount.

There are **two kinds of outputs**:

| Kind | Columns | What actually decides them |
|---|---|---|
| **Capacity** (hard numbers from the 90-day cash walk) | `amount_safe_to_pay`, `earliest_date_for_full_payment` | The daily register. If the forecast is wrong, these two move. |
| **Recommendation** (rules on top of those numbers) | `affordability_status`, `recommended_payment_method`, `payment_plan`, `spending_changes_needed` | Eligibility gates + ranking. These are almost solved on the 25 samples. |

A wrong capacity number can still flip a recommendation (`request_13`, `request_21`). A right recommendation can still have a wrong rupee amount (`request_02`–`request_08`, …).

---

## 0. One picture of the whole path

```text
dataset CSVs
    │
    ▼
[once] load_indexes     put every CSV into dicts (not a graph)
[once] fill_images      blank event.amount → RapidOCR → Gemini vision if still blank
    │
    ▼
for THIS request only (request_01, then request_02, …):
    assemble_context    attach THIS user's profile, events, messages, seller options
    parse_messages      Gemini → facts (salary change, ignore bonus, stop projecting, …)
    normalize_ledger    filter events → FX → invent next 90 days of repeats → walk cash
                        → amount_safe_to_pay + earliest_date_for_full_payment
    decide              try legal plans (full / partial / EMI / wait / cuts) → rank one
    persist             save code/data_layer/contexts/<request_id>.json
    │
    ▼
[once] verify           contract checks → output.csv
```

`request_date` is **today for that row**. `current_available_balance` is cash **that morning**. User 01’s today is `2024-03-03`. User 06’s today is `2026-01-03`. We never use the real calendar.

The **90-day** window is required by the problem statement (“90-Day Safety Check”). It is not a number we invented.

---

## 1. What is attached to one request (data acquired)

Nothing is decided yet. This step only **joins**.

| File | What we take | Used later for |
|---|---|---|
| `requests.csv` / sample row | today, requested amount, deadline, partial allowed, question text | every output |
| `financial_profiles.csv` | cash, minimum floor, protect / reduce / stop lists, allowed methods, max EMI months | safety walk + which plans are legal |
| `financial_events.csv` | that user’s history + pending + next confirmed salary | the daily register |
| `messages.csv` | employer / bank / merchant notes for that user or request | amend salary, ignore a bonus, stop projecting |
| `images.csv` + `media/images/` | receipt PNG when `amount` is blank | fill the missing debit/credit |
| `request_payment_options.csv` | seller EMI offers for this `request_id` | clone into `payment_plan` if EMI wins |
| `exchange_rates.csv` | dated FX | convert event currency → `home_currency` |

Example, **request_06** (EUR 620.40 investment, today `2026-01-03`, deadline `2026-01-14`):

- Profile: balance `1942.40`, floor `800`, methods `{full_payment, partial_payment}`, can stop `{streaming}`, protect `{rent, insurance, transport}`.
- Events: rent, salary, groceries, streaming `event_476` (EUR 19, stoppable), …
- Message: “temporary monthly pay is EUR 1037.52 … next payroll.”
- No EMI needed if today-full-with-cut works.

---

## 2. Filtering — what is dropped, and what that means

An event is a **row in `financial_events.csv`**. Filters look at **that row’s columns**, not at the user’s question.

Cash day = `settlement_date` if present, else `event_date`.

### 2.1 Drop before the walk (not cash)

| If the row looks like this | We do | Why | Example |
|---|---|---|---|
| `status` = `cancelled` or `failed` | drop | never hit the account | a declined card charge |
| `direction` = `non_cash` or `status` = `unrealized` | drop | paper value, not spendable cash | portfolio mark-to-market |
| `amount` still blank after OCR/Gemini | drop | blank is **not** zero | a photo we could not read |
| `status` = `pending` **and** `direction` = `credit` | drop | bonus / refund / prize not in the account | “refund initiated” |
| `status` = `pending` **and** `direction` = `debit` | **keep** | money is reserved | a hold for a hotel |
| message says ignore unconfirmed credit, and this is a non-salary unsettled credit | drop | gig/bonus not withdrawable | QuickCrew “payout still pending” |
| message says internal transfer | drop transfer in/out | moving between own accounts is not income | “transfer between your two accounts” |
| description is bonus / commission / arrears / refund / prize | do not **project** it again | one-off, not a repeating bill | “Performance commission” |
| description is “final employer payroll” | do not invent later salaries | job ended | request_05 |

History (cash day **before** today) is **not** dropped. It is not added to the 90-day walk as a cash hit. It is only used to detect “rent hits on the 2nd every month.”

Future / same-day rows that survive become **explicit flows**.

### 2.2 What a message is allowed to change

Gemini reads every message (English or Indonesian). It returns facts. It does **not** return a payment method.

| Fact | Meaning in the walk |
|---|---|
| `salary_amount` / `salary_date` | next payroll uses this figure / this day |
| `stop_salary_projection` | do not invent future salaries |
| `ignore_unconfirmed_credit` | pending credits (and unconfirmed gig pay) are not cash |
| `rent_increase_pct` | next rents are uplifted |
| `confirmed_invoice_amount` + date | count that one credit; ignore other pending invoices |
| `one_off_credit` | extra cash once, on the stated day |
| embedded “pay a fee to release a prize” | **ignored** (instruction, not a fact) |

### 2.3 FX

Output is in `home_currency`. An IDR grocery on an INR user is converted with the rate on that cash day (exact date, else latest on or before, else invert the pair).

---

## 3. Building the 90-day register (this is where X and earliest live)

Two lists are merged:

1. **Explicit** — surviving events on/after today (including “next confirmed salary” already in the CSV).
2. **Projected** — we look at history and book the *next* occurrences inside 90 days.

Do **not** project shopping, windfalls, or refunds.

| History pattern | What we book going forward |
|---|---|
| ~monthly (25–35 day median) | rent, utilities, salary, loans, subscriptions on that day-of-month |
| Salary hits two calendar days (7th and 20th), each ≥3 times | two monthly salary series (freelance retainers) |
| Weekly groceries / transport (5–10 days) | keep repeating |
| Weekly **fixed** dining | **only the next meal**, not 90 days of dinners |
| ~21-day **reducible** dining / entertainment | keep repeating (needed so `reduce_to:` has a future event) |
| Biweekly dining that is fixed | do not project |
| Weekly gig salary + “payout not withdrawable” | do **not** project |

Variable groceries/transport use the **median** of the last few amounts (not an outlier week).

Same `(date, category, credit/debit)` from an explicit row wins over a projection. We do not double-count payday.

### 3.1 The safety walk

Start:

```text
balance = current_available_balance
floor   = minimum_balance_to_keep
day     = request_date
end     = request_date + 90 days
```

Each day: add that day’s signed flows (credits +, debits −), then add any **candidate payment** we are testing.

**Safe** = every day’s balance ≥ floor.

Toy numbers (same idea as request_06):

```text
today 2026-01-03
balance 1942.40
floor     800.00
requested 620.40

Without paying the request, the lowest point in 90 days is 1408.76
room = 1408.76 − 800 = 608.76
```

That **room** is the most you can pay **today** without the walk going under 800.

### 3.2 `amount_safe_to_pay` (X) — binary search, not a guess

Keep this definition:

> Largest amount payable **today** such that the 90-day walk never goes below the floor. Then cap at `requested_amount`. Computed **before** optional spending cuts.

Algorithm:

1. Try paying the **full** request today. If the walk stays ≥ floor → X = requested amount.
2. Else binary-search between `0` and requested (0.01 steps) for the largest today-payment that still stays ≥ floor.

So if trough-without-payment is `T` and floor is `F`:

```text
X ≈ min(requested, max(0, T − F))
```

Cuts are **not** applied when computing X. Gold `request_06` X is `603.3` even though the recommended plan is “stop streaming, then pay 620.40 today.”

### 3.3 `earliest_date_for_full_payment`

Also **before cuts**. Independent of whether the user even accepts `full_payment`.

For each day `D` from today through today+90:

- pretend the **full** request is paid on `D` (one lump)
- if that walk is safe → that `D` is the earliest; stop

Rules:

- If status is `affordable_now`, the output date **must** equal `request_date`.
- If no day works, leave the field **empty**.
- The date may be **after** today even when we still recommend “pay today with a cut” (`request_06`: pay today after stopping streaming; uncut earliest is payday `2026-01-15`).

This is why X and earliest move together. Both ask the same walk. Different question:

| Question | Column |
|---|---|
| How much can I take out **this morning**? | X |
| First morning I can take out the **whole** request in one hit? | earliest |

---

## 4. Recommendation — legal plans, then one winner

`decide.py` does **not** recompute X. It only asks “is this schedule safe on this (possibly cut) register?”

### 4.1 Spending changes (optional, only to unlock a plan)

Tried on a copy of the flows. Legal only if **all** of these hold:

- the event is a **future debit** in the forecast
- category is **not** in `expense_categories_to_protect`
- event `flexibility` allows it (`stoppable` / `reducible` / `reducible_or_stoppable`)
- category is in the user’s willing-to-stop or willing-to-reduce list
- `reduce_to` uses `minimum_allowed_amount` (never invented)
- stop and reduce of the **same** event are mutually exclusive

Output strings:

```text
none
stop:event_476
reduce_to:event_989:665950
stop:event_1815|reduce_to:event_1816:23.50
```

We try: no cuts, each single cut, then a few two-event combos.

X and earliest **stay on the uncut walk** even if the winner uses a cut.

### 4.2 Which methods may be proposed

An immediate method is legal only if it is in `payment_methods_user_will_consider`.

| Method | Extra gates | What `payment_plan` looks like |
|---|---|---|
| `full_payment` | user accepts it; one debit of the **full** request today is safe (with or without a legal cut) | `2026-01-03:620.40` |
| `partial_payment` | request allows partial; user accepts it; `0 < X < requested`; earliest ≤ deadline; two-hit schedule is safe | `today:X\|earliest:requested−X` (constructed, not from the options file) |
| `installments` | user accepts EMI **and** `max_installment_months` is set; last EMI ≤ deadline; span ≤ cap; cloned schedule is safe | **exact clone** of one `request_payment_options.csv` row |
| `wait` | user accepts `full_payment`; earliest is **after today** and **≤ deadline**; one debit on earliest is safe | `2024-06-15:12693000` |
| `not_recommended` | nothing else is safe | `none` |

Blank `max_installment_months` = EMI off, even if the seller offered rows.

### 4.3 Ranking (first difference wins)

Among safe, eligible plans:

1. Completes the full request by the deadline
2. Requires **no** spending changes
3. Lower total paid (EMI fees lose to cheaper cash)
4. Earlier first payment
5. Fewer payments
6. Fewer change actions
7. Lowest `payment_option_id`

So: if today-full with **no** cuts is safe, that always beats today-full **with** cuts. That is why a slightly-too-high X turns `request_21` into `affordable_now` / `none` instead of “stop cloud + reduce streaming.”

### 4.4 Status is a label on the winning method

| Winner | Status |
|---|---|
| `full_payment` and cuts = `none` | `affordable_now` |
| `full_payment` with cuts, or `partial_payment`, or `installments` | `affordable_with_plan` |
| `wait` | `affordable_later` |
| `not_recommended` | `not_affordable` |

---

## 5. Which process owns which output

```text
events + messages + FX + recurrence
        │
        ▼
   90-day uncut walk
        │
        ├──────────────► amount_safe_to_pay          (binary search, today)
        ├──────────────► earliest_date_for_full_payment (first day full lump is safe)
        │
        ▼
   try cuts on copies of the walk
        │
        ▼
   legal plans (methods ∩ safety ∩ deadline)
        │
        ▼
   rank
        │
        ├──────────────► recommended_payment_method
        ├──────────────► affordability_status
        ├──────────────► payment_plan
        └──────────────► spending_changes_needed
```

| Output | Major factors | Preprocessed data |
|---|---|---|
| **X** | starting cash, floor, every future debit/credit we booked, **no cuts** | filtered events, projected repeats, FX, message facts |
| **Earliest** | same walk; first day a full lump fits | same as X |
| **Method** | allowed methods, deadline, whether today-full / EMI / wait / partial is safe, ranking | profile methods, seller options, X, earliest, cuts |
| **Status** | 1-to-1 with the winning method (+ whether cuts were needed) | winner only |
| **Plan** | clone EMI row, or construct partial from X+earliest, or one date:amount | options file or X/earliest |
| **Changes** | only if a cut was required to make the winner safe, and no-cut lost on rank | future flexible debits ∩ user lists |

---

## 6. Two worked paths

### 6.1 request_01 — pay today, no drama

- Today `2024-03-03`, laptop ZAR 25,256, floor ZAR 18,000, user accepts `full_payment`.
- Uncut walk: paying 25,256 today never goes under 18,000.
- X = 25,256. Earliest = today.
- Plans: today-full with no cuts is legal → rank picks it.
- Output: `affordable_now`, `full_payment`, `2024-03-03:25256`, `none`.

### 6.2 request_06 — X a bit short; a cut unlocks today

- Today `2026-01-03`, invest EUR 620.40, floor EUR 800, can stop streaming.
- Uncut walk: room today is **608.76** (gold 603.30). Full 620.40 today would break the floor (next streaming EUR 19 is in the register).
- X = 608.76 (not 620.40). Uncut earliest = `2026-01-15` (payday).
- Partial is off (user/request). Wait on `2026-01-15` is **after** deadline `2026-01-14` → illegal.
- Copy the register, `stop:event_476` (family streaming). Today-full 620.40 is now safe.
- Rank: completing by the deadline **with** a cut beats “cannot complete.”
- Output: X still 608.76 (uncut), earliest still `2026-01-15` (uncut), method `full_payment`, status `affordable_with_plan`, plan `2026-01-03:620.40`, changes `stop:event_476`.

That is the intended split: **numbers from the raw walk, action from the ranked plan.**

---

## 7. Current sample run — what is wrong

25 gold rows. Official compare is **exact match per column**. Close X still counts as wrong for that column.

### 7.1 Recommendation columns (almost solved)

| Column | Right | Wrong |
|---|---|---|
| `recommended_payment_method` | 24 | 1 |
| `affordability_status` | 23 | 2 |
| `payment_plan` | 22 | 3 |
| `spending_changes_needed` | 22 | 3 |

**Wrong recommendation (decision flips):**

| Request | We said | Gold said | Why |
|---|---|---|---|
| **request_13** | `affordable_now` / `full_payment` / pay `2024-03-07:941.60` | `affordable_later` / `wait` / `2024-05-15:941.60` | Uncut walk is too optimistic. We think today-full is safe. Gold’s X is 433.4, so they wait for 15 May salary. |
| **request_21** | `affordable_now` / `full_payment` / `none` | `affordable_with_plan` / `full_payment` / `stop:event_1815\|reduce_to:event_1816:23.50` | Our X hit the full request (1574.40 vs gold 1543.35, only **+2.0%**). Rank then correctly prefers “no cuts.” The miss is the register being ~31 too rich, not the ranker. |

**Right method/status, wrong extra fields:**

| Request | Miss | We | Gold |
|---|---|---|---|
| **request_11** | changes + earliest | also `stop:event_949`; earliest `2025-06-15` | only `reduce_to:event_989:665950`; earliest `2025-07-15` |
| **request_12** | **exact** | seasonal pay still projected | all six fields match gold |
| **request_18** | plan + earliest | wait `2026-08-15` | wait `2026-09-15` (same idea, one payday early) |
| **request_19** | plan only | partial `35973.18` + `3686.82` | partial `28820` + `10840` (same two dates; amounts follow our higher X) |

### 7.2 Earliest full-pay date — how far off

19 / 25 exact (including both-empty, which is a match).

The 6 misses; **delta = our date minus gold date** (negative = we are early):

| Request | Today | Gold earliest | Our earliest | Days off | vs today |
|---|---|---|---|---|---|
| request_07 | 2024-09-05 | 2024-10-23 | 2024-10-15 | **8 days early** | gold +48d, we +40d (we landed on payday; gold is later in the month) |
| request_11 | 2025-05-03 | 2025-07-15 | 2025-06-15 | **30 days early** | one extra month of salary in our walk |
| request_12 | 2026-04-05 | 2026-04-05 (today) | 2026-04-05 | exact | seasonal contract-ended no longer zeros income |
| request_13 | 2024-03-07 | 2024-05-15 | 2024-03-07 | **69 days early** | same bug as the decision flip — we think today works |
| request_18 | 2026-07-07 | 2026-09-15 | 2026-08-15 | **31 days early** | one payday too soon |
| request_21 | 2026-04-03 | 2026-04-15 | 2026-04-03 | **12 days early** | we think today-full is safe; gold waits until payday for the *uncut* earliest |

Pattern: when we miss, we are **early or empty**, never late. Median miss among the dated misses is **~21 days**. Four of the six are “we believed an earlier payday.”

`request_date` itself was never wrong. Today is taken from the CSV. The miss is **which future morning the lump-sum becomes safe.**

### 7.3 Amount safe to pay today — how close

3 / 25 exact. The other 22 fail exact match. Closeness vs gold X:

| Band | Count | Requests |
|---|---|---|
| Exact | 3 | 01, 09, 16 |
| Within 1% | 5 | those three + **06** (+0.9%), **22** (+1.0%) |
| Within 5% | 9 | + 21 (+2.0%), 17 (+2.4%), 11 (−2.5%), 02 (+3.7%) |
| Within 10% | 12 | + 12 (−6.2%), 23 (+7.5%), 07 (+9.8%) |
| Within 25% | 16 | + 10 (−11.9%), 08 (+18.4%), 18 (+19.7%), 19 (+24.8%) |
| Worse than 25% | 9 | see below |

Direction: **16 too high**, **6 too low**, **3 exact**. Median miss ≈ **12% of gold X**. Mean miss ≈ **29%** (skewed by the bad tail). Versus the *requested bill*, median miss is only **~2%**.

**Worse than 25% (the ones to fix in the register):**

| Request | Gold X | Our X | vs gold X | vs requested | Notes |
|---|---|---|---|---|---|
| request_13 | 433.40 | 941.60 | **+117%** | +54% of request | also flips the decision |
| request_05 | 737 | 0 | **−100%** | −4.8% of request | no salary (job ended); we over-drain |
| request_14 | 597.74 | 0 | **−100%** | −11% of request | same shape as 05 |
| request_25 | 1,425,000 | 2,427,517 | **+70%** | +1.7% of request | large IDR, decision still right |
| request_20 | 5,400 | 8,469 | **+57%** | +1.0% of request | decision still right |
| request_03 | 873,000 | 1,305,317 | **+50%** | +7.9% of request | wait date is already correct |
| request_04 | 8,401,800 | 12,167,651 | **+45%** | +30% of request | wait date is already correct |
| request_15 | 83.05 | 51.40 | **−38%** | −0.9% of request | decision still right |
| request_24 | 13,420 | 17,820 | **+33%** | +4.0% of request | decision still right |

**Already tight (same decision, tiny X gap):** 06, 22, 21, 17, 11, 02.

### 7.4 Flag list (this run)

Use this as the punch list. “Decision” = status + method + plan + changes.

| Request | Decision | X | Earliest | Priority |
|---|---|---|---|---|
| 13 | **WRONG** (pay today vs wait) | +117% | 69d early | fix register drain |
| 21 | **WRONG** (missing required cuts) | +2% / +31 | 12d early | need ~31 more uncut drain |
| 11 | extra `stop:cloud` | −2.5% | 30d early | reduce-only should win; later earliest |
| 12 | **exact** (seasonal contract-ended is not a full income stop) | exact | exact | keep |
| 18 | wait one month early | +20% | 31d early | extra payday in the walk |
| 19 | partial amounts follow our X | +25% | exact | X only |
| 07 | OK | +10% | 8d early | payday vs 10-23 |
| 05, 14 | OK (`not_recommended`) | we say 0 | empty=empty | leftover room of a few hundred |
| 03, 04, 20, 24, 25 | OK | +33% to +70% | exact or empty=empty | overstated room, decision held |
| 02, 06, 08, 10, 15, 17, 22, 23 | OK | within ~20% | exact | polish only |
| 01, 09, 16 | **all six exact** | exact | exact | — |

---

## 8. What to change next (and what not to)

Do **not** change the binary-search definition of X. Do **not** rewrite the ranker. Those are not the leak.

Tighten the **daily register** so X and earliest move toward gold:

1. **request_13 / 21** — missing near-term drain (today looks too safe).
2. **request_05 / 14** — no-income users still over-drained to X = 0.
3. **Large IDR overshoots (03, 04, 25, 20, 24)** — variable spend / one extra bill still too light; decisions already match.
4. **Early paydays (07, 11, 18)** — we treat a salary date as enough; gold needs one more cycle.

Gemini stays out of this. The walk in `ledger.py` is the only place those four move.

---

## 9. Answers to the pending / message / request-text questions

**Pending rows are tallied — with a hard split.**

- `pending` **debit** on/after today → reserved cash, it **is** in the walk.
- `pending` **credit** → ignored (problem statement: ignore pending credits).
- `scheduled` salary / bills on/after today → explicit flows, not projections.
- A hold that `linked_event_id`s to a later settled/scheduled row is dropped so we do not pay twice.

**Not every historical amount is replayed.** History before today is only for cadence. We do **not** book shopping, windfalls, refunds, commissions, or 90 days of fixed biweekly dining. Those are one-offs or too noisy. The safety walk then uses only the booked register — X and earliest are binary-search / day-scan on that register, not guesses.

**Messages do change numbers** (salary, rent %, ignore bonus, stop projecting, invoice). They do **not** pick the payment method. `new_recurring_expense_note` (childcare) now books a monthly debit if a matching event exists. `request_text` is a plain string already split into columns (`requested_amount`, dates). We do not send it to Gemini for the decision. The explanation is written from the ranked plan.

**Batch vs sequential.** One `python code/main.py` already processes all 250 in one process (that is the batch). Inside that run, requests are decided one-by-one so a bug in request_40 cannot corrupt request_41. OCR/VLM is shared once. Gemini facts are cached. Parallelizing the 250 would not save API quota.
