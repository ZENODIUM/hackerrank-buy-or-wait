# Evaluation workflow

| Command | What it does |
|---|---|
| `python code/main.py --mode eval` | Score all 250 `dataset/requests.csv` rows. Write root `output.csv` and this folder’s `usage_report.md`. |
| `python code/main.py --mode samples` | Development only: 25 public samples. Writes `code/data_layer/sample_alignment.md`. Gold output columns are not used until that compare. |
| `python code/evaluation/main.py` | Contract check of the current `output.csv` (schema, one row per request, `0 ≤ X ≤ requested`). Print the usage report. No hidden labels. |

`usage_report.md` counts **Google Gemini tokens in this pipeline** (messages / rare vision). It does not count Cursor or IDE tokens. Decisions and `amount_safe_to_pay` are Python (0 tokens).
