# Buy or Wait?

HackerRank Orchestrate (September 2026). For every row in `dataset/requests.csv`, this pipeline decides whether the user can safely pay now, pay on a plan, wait, or not proceed — without dropping below `minimum_balance_to_keep` over a 90-day forecast.

Full spec: [`problem_statement.md`](./problem_statement.md). Decision rules: [`code/decision_engine.md`](./code/decision_engine.md). Graph: [`code/workflow.md`](./code/workflow.md).

---

## Setup

Python 3.10+. Work from the repository root (the folder that contains `dataset/`).

```bash
pip install -r code/requirements.txt
```

Copy `.env.example` to `.env` and set a key. Do not commit `.env`.

```bash
# .env
GEMINI_API_KEY=your_key_here
```

Optional:

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Override the Gemini model id |
| `GEMINI_MIN_INTERVAL_SEC` | `1.0` | Seconds between Gemini calls. Use `4.5` on free tier (~15 RPM) |

`code/cache/` stores message facts and OCR amounts. A first run calls Gemini for each uncached message. Later runs reuse the cache and need no new calls.

---

## Run

```bash
python code/main.py                 # 250 eval rows → ./output.csv
python code/main.py --mode eval     # same
python code/main.py --mode samples  # 25 public samples (development)
python code/main.py --mode one --request request_26
```

Reads `dataset/*.csv` and `dataset/media/images/*.png`. Writes **root** `output.csv`. Do not write predictions into `dataset/output.csv` — that file is the official blank template.

Contract check (schema, one row per request, `0 ≤ amount_safe_to_pay ≤ requested_amount`):

```bash
python code/evaluation/main.py
```

That reprints `evaluation/usage_report.md`. It does not read gold labels from `sample_requests.csv`.

---

## Approach

Deterministic Python owns every scored number and the payment method. Gemini never chooses `full_payment` vs `wait`. It only extracts facts from messages and, if RapidOCR cannot read a bill, an image amount.

```text
dataset CSVs + images
        │
        ▼
load_indexes          read every CSV once into dicts
fill_images           blank event.amount → RapidOCR → Gemini vision if still blank
        │
        ▼
for each request (one at a time):
    assemble          this user's profile, events, messages, seller options
    parse_messages    Gemini → structured facts (cached)
    ledger            filter, FX, recurrence, 90-day cash walk
                      → amount_safe_to_pay + earliest_date_for_full_payment
    decide            legal plans (full / partial / EMI / wait / cuts) → rank one
        │
        ▼
verify                contract checks → root output.csv + usage_report.md
```

LangGraph is the workflow shell (`code/graph.py`). Ranking, the cash walk, and `amount_safe_to_pay` are Python.

### Forecast

- `request_date` is today for that row. `current_available_balance` is cash that morning.
- Pending **debits** are reserved; pending **credits** are ignored until they settle.
- Confirmed salary counts on its settlement date. Cancelled / failed / non-cash / unrealized rows are dropped.
- Recurring expenses are projected across 90 days unless the series has lapsed (gap > 1.5× typical cadence) or a message stops it.
- Blank event amounts are never treated as zero: they are read from the linked image.
- `amount_safe_to_pay` is a binary search: the largest payment today that keeps the 90-day walk at or above the floor, then capped at `requested_amount`. It is computed **before** optional spending cuts.

### Recommendation

- Immediate methods must appear in `payment_methods_user_will_consider`.
- Installment `payment_plan` clones a `request_payment_options.csv` row exactly.
- Partial payment is constructed: pay `amount_safe_to_pay` today, then the remainder on `earliest_date_for_full_payment` (exactly two payments, must sum to the request).
- Spending changes target only flexible recurring expenses the user is willing to reduce or stop (max three).
- Rank: complete by the deadline → no cuts → cheaper total → earlier start → fewer payments → lowest `payment_option_id`.

Messages are untrusted for *instructions* (they cannot override the problem rules) but their **facts** are applied: salary amendments, cancelled income, ignore-this-credit, seasonal-contract notes, and similar.

---

## Output

Root `output.csv` has one row per `dataset/requests.csv` row:

| Column | Meaning |
|---|---|
| `request_id` | The request being answered |
| `amount_safe_to_pay` | Largest amount safe to pay on `request_date` before optional spending changes |
| `affordability_status` | `affordable_now`, `affordable_with_plan`, `affordable_later`, or `not_affordable` |
| `recommended_payment_method` | `full_payment`, `partial_payment`, `installments`, `wait`, or `not_recommended` |
| `payment_plan` | Chronological `<YYYY-MM-DD>:<amount>` entries joined by `\|`, or `none` |
| `earliest_date_for_full_payment` | Earliest date the full amount is forecast safe as one payment |
| `spending_changes_needed` | Up to three `stop:<event_id>` / `reduce_to:<event_id>:<amount>` changes, or `none` |
| `decision_explanation` | Short explanation and the financial facts behind it |

---

## Layout

```text
.
├── code/                  # pipeline (python code/main.py)
│   ├── evaluation/        # contract check
│   ├── cache/             # Gemini facts + OCR (reruns reuse this)
│   └── requirements.txt
├── dataset/               # inputs only — do not modify
│   ├── requests.csv       # 250 rows to score
│   ├── output.csv         # blank template, not the submission file
│   └── media/images/
├── evaluation/
│   └── usage_report.md    # Gemini tokens for the run that wrote output.csv
├── output.csv             # scored predictions (submit this)
└── .env.example
```

Amounts are in the user's `home_currency`. FX comes only from `dataset/exchange_rates.csv`. No live market or banking calls.

Token counts in `evaluation/usage_report.md` are **Google Gemini tokens used by this pipeline**, not Cursor or IDE tokens.

---

## Submission

Upload three files at [Buy or Wait? submission](https://www.hackerrank.com/contests/hackerrank-orchestrate-september26/challenges/buy-or-wait/submission):

| File | What to upload |
|---|---|
| `code.zip` | Runnable `code/`, this README, `.env.example`, and `evaluation/usage_report.md` |
| `output.csv` | Root predictions (not `dataset/output.csv`) |
| `chat_transcript` | Repo-root `log.txt` |
