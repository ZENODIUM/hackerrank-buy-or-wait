"""Terminal entry point: python code/main.py"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CODE_DIR.parent
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from graph import PIPELINE  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Buy or Wait? pipeline")
    parser.add_argument(
        "--mode",
        choices=("eval", "samples", "one"),
        default="eval",
        help="eval=requests.csv (250), samples=sample_requests.csv (25), one=single id",
    )
    parser.add_argument("--request", help="request_id when --mode one")
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Print a JSON debug view of assembled context(s)",
    )
    parser.add_argument(
        "--dump-limit",
        type=int,
        default=1,
        help="How many contexts to dump (default 1)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode == "one" and not args.request:
        print("error: --mode one requires --request", file=sys.stderr)
        return 2

    state = PIPELINE.invoke(
        {
            "repo_root": str(REPO_ROOT),
            "mode": args.mode,
            "request_id": args.request or "",
        },
        {"recursion_limit": 400},
    )

    stats = state.get("stats") or {}
    from llm_gemini import api_key, model_name

    print("Buy or Wait?")
    print(f"repo: {REPO_ROOT}")
    print(f"mode: {args.mode}")
    print(f"gemini_model: {model_name()}")
    print(f"gemini_key: {'set' if api_key() else 'missing'}")
    skip = {"sample_mismatches", "contract_errors"}
    for key, value in stats.items():
        if key in skip:
            continue
        print(f"{key}: {value}")
    mismatches = stats.get("sample_mismatches") or []
    if mismatches:
        print("sample_mismatches:")
        for item in mismatches:
            print(f"  {item}")
    errors = stats.get("contract_errors") or []
    if errors:
        print("contract_errors:")
        for item in errors:
            print(f"  {item}")

    contexts = state.get("contexts") or []
    if args.dump and contexts:
        payload = [context.to_debug_dict() for context in contexts[: args.dump_limit]]
        print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
