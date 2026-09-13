# Buy or Wait — solution

See the repository-root [`README.md`](../README.md) for setup, run commands, and the approach overview.

Deterministic Python pipeline. Gemini is used only to read messages and (if RapidOCR fails) bill images. It does not pick the payment method.

## Setup

From the repository root (the folder that contains `dataset/`):

```bash
pip install -r code/requirements.txt
```

Copy `/.env.example` to `.env` and set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`). Do not commit `.env`.

Cached facts in `code/cache/` let a rerun finish without new Gemini calls. A first run without cache calls Gemini for each uncached message.

## Run

```bash
python code/main.py
python code/main.py --mode eval      # same: 250 rows → ./output.csv
python code/main.py --mode samples   # 25 public samples + alignment
python code/main.py --mode one --request request_26
```

Reads `dataset/*.csv` and `dataset/media/images/*.png`. Writes `output.csv` in the repo root.

## Evaluation workflow

```bash
python code/evaluation/main.py
```

That checks `output.csv` against `dataset/requests.csv` (schema, ids, bounds) and reprints `evaluation/usage_report.md`. It does not read gold labels from `sample_requests.csv`.

`--mode samples` is the optional development compare (input columns only until the compare step).

Token usage in `evaluation/usage_report.md` is Gemini usage inside this pipeline, not Cursor/IDE tokens.
