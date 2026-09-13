# Token usage report

Final full-dataset run that produced root `output.csv`.

## Scope

- These counts are **Google Gemini tokens used by this Buy or Wait pipeline** (message facts and, if needed, vision on unread bills).
- They do **not** include Cursor, IDE, or chat-agent tokens.
- Ranking, the 90-day cash walk, and `amount_safe_to_pay` are deterministic Python (0 tokens).

## Models

- Provider: google
- Model: `gemini-3.5-flash-lite`
- Models used: 1 (no second model)
- This-process Gemini: cache reuse of the live extract below
- Role: Gemini reads every uncached message; Gemini vision only if RapidOCR leaves an amount blank

## Totals (Gemini workflow that produced these predictions)

- Model calls: 197
- Input tokens: 54196
- Output tokens: 5350
- Total tokens: 59546
- Requests scored: 250
- Average tokens per request: 238.1840
- Estimated cost: $0.0076 (Gemini Flash-Lite list: $0.10/1M in, $0.40/1M out)
- Estimated cost per request: $0.000030

## This process (the eval that wrote output.csv)

- Model calls: 0
- Input tokens: 0
- Output tokens: 0
- Total tokens: 0

## Notes

- API key is read from `GEMINI_API_KEY` or `GOOGLE_API_KEY` (never written to this file or to git).
- Cached message facts and OCR amounts make a later eval show 0 new calls. That is reuse of the live extract, not an outage.
- There is no regex fallback. Uncached messages call Gemini; cached ones apply stored JSON facts.
- RapidOCR handles printed bills; Gemini vision is used only when OCR cannot read an amount.

## Hard failures

- Gemini call/parse failures this process: 0
- Messages that returned no JSON this process: 0
- A hard failure is not cached as a usable fact. The next run retries that message.
- A successful `{}` (nothing usable in the text) is a call, not a failure.

## Run stats

- mode: eval
- output: output.csv
- contract_errors: 0
- message_llm_failures: 0
- message_llm_calls: 0
- message_cache_hits: 198
