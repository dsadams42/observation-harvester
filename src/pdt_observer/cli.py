from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from pdt_observer.agent import load_task, run_agent_investigation, run_offline_demo
from pdt_observer.config import MissingAPIKeyError
from pdt_observer.validation import ObservationValidationException


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdt-observer",
        description="Run a bounded PDT observation-harvesting investigation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="Run the deterministic offline Milltown demo.")

    investigate = subparsers.add_parser("investigate", help="Run the OpenAI Agents SDK path.")
    investigate.add_argument("task_file", type=Path, help="Path to an InvestigationTask JSON file.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "demo":
            result = run_offline_demo()
        elif args.command == "investigate":
            task = load_task(args.task_file)
            result = run_agent_investigation(task)
        else:
            parser.error(f"unknown command: {args.command}")
    except MissingAPIKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ObservationValidationException as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1

    print(result.model_dump_json(indent=2))
    return 0


def console() -> int:
    return main()
