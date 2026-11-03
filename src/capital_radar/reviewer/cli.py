"""CLI för granskaren: `python -m capital_radar review ...`."""

from __future__ import annotations

import argparse
from typing import Any, Sequence

from ..config import Settings
from ..db import Database
from . import prompts
from .service import build_review_service

_TASKS = {
    "extractions": prompts.EXTRACTION_TASK,
    "headlines": prompts.HEADLINE_TASK,
}


def add_review_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "review",
        help="Granska DeepSeeks extraction eller bortfiltrerade rubriker",
    )
    parser.add_argument("target", choices=sorted(_TASKS))
    parser.add_argument(
        "-n", "--limit", type=int, default=10,
        help="extractions: antal jobb; headlines: antal rubriker (standard 10)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Visa indata och tokenestimat utan API-anrop",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Försök igen med jobb som slutade i timeout/rate limit/fel",
    )
    parser.add_argument(
        "--max-print-chars", type=int, default=6000,
        help="Max tecken av indata som skrivs ut per jobb vid --dry-run",
    )
    parser.set_defaults(handler=run_review_command)


def run_review_command(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    db = Database(settings.db_path)
    db.initialize()
    service = build_review_service(settings, db=db)
    result = service.run(
        _TASKS[args.target],
        limit=max(1, args.limit),
        dry_run=args.dry_run,
        retry_failed=args.retry_failed,
    )
    print(format_summary(result))
    if args.dry_run:
        for item in result["prepared"]:
            label = (
                f"extraction_job {item.extraction_job_id} (dokument {item.document_id})"
                if item.extraction_job_id is not None
                else f"rubrikbatch med {len(item.targets)} dokument"
            )
            print(f"\n=== {label}: ~{item.estimated_input_tokens} tokens in ===")
            text = item.user
            if len(text) > args.max_print_chars:
                text = text[:args.max_print_chars] + f"\n[…{len(item.user) - args.max_print_chars} tecken till]"
            print(text)
    return 0


def format_summary(result: dict[str, Any]) -> str:
    cost = result.get("estimated_cost_usd")
    lines = [
        f"Uppgift: {result['task']}  modell: {result['model']}  status: {result['status']}",
        f"Jobb: {result['jobs']}  estimat: ~{result['estimated_input_tokens']} tokens in, "
        f"~{result['estimated_output_tokens']} ut"
        + (f", ~${cost:.4f}" if cost is not None else ", pris okänt för modellen"),
    ]
    if result["status"] == "disabled":
        lines.append(result["reason"])
    if result.get("counts"):
        lines.append("Utfall: " + ", ".join(f"{k}={v}" for k, v in sorted(result["counts"].items())))
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m capital_radar")
    add_review_parser(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    return args.handler(args)
