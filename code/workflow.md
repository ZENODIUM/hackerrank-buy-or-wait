# Buy or Wait? workflow

Shared data load runs once. Each request then walks assemble → facts → ledger → decide **alone** before the next request starts.

**Default model:** `gemini-3.5-flash-lite`. Key: `GEMINI_API_KEY` in `.env`. Calls are spaced 1.0s apart (paid default; set `GEMINI_MIN_INTERVAL_SEC=4.5` on free tier) and cached. 429 retries back off so either tier works. Token use is unchanged.

See `code/decision_engine.md` for the filter and ranking rules.

```mermaid
flowchart TD
    start([python code/main.py]) --> load
    load["load_indexes — read CSVs once"]
    images["fill_images — RapidOCR, then Gemini if still blank"]
    load --> images --> loop

    subgraph perRequest [One request at a time]
        loop["process_one"]
        assemble["assemble_context for this request_id"]
        facts["parse_messages: Gemini only, cached + throttled"]
        ledger["normalize_ledger: filter, FX, recurrence, 90-day walk"]
        decide["decide: legal plans + rank"]
        loop --> assemble --> facts --> ledger --> decide
        decide -->|queue not empty| loop
    end

    decide -->|queue empty| verify
    verify["verify — contract checks"]
    verify --> out([output.csv + usage_report.md])
```
