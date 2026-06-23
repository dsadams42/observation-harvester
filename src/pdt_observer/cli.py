from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pdt_observer.agent import load_task, run_agent_investigation, run_offline_demo
from pdt_observer.config import MissingAPIKeyError
from pdt_observer.models import InvestigationRun
from pdt_observer.validation import ObservationValidationException, validate_run


def load_run(path: Path) -> InvestigationRun:
    return InvestigationRun.model_validate_json(path.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdt-observer",
        description="Run a bounded PDT observation-harvesting investigation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="Run the deterministic offline Milltown demo.")

    validate = subparsers.add_parser(
        "validate",
        help="Validate a Codex-produced investigation run JSON file without API access.",
    )
    validate.add_argument("run_file", type=Path, help="Path to an InvestigationRun JSON file.")

    summarize = subparsers.add_parser(
        "summarize",
        help="Print a short human-readable summary of an investigation run.",
    )
    summarize.add_argument("run_file", type=Path, help="Path to an InvestigationRun JSON file.")

    investigate_api = subparsers.add_parser(
        "investigate-api",
        help="Run the optional OpenAI Agents SDK path with OPENAI_API_KEY.",
    )
    investigate_api.add_argument(
        "task_file",
        type=Path,
        help="Path to an InvestigationTask JSON file.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "demo":
            result = run_offline_demo()
            print(result.model_dump_json(indent=2))
            return 0
        if args.command == "validate":
            run = load_run(args.run_file)
            report = validate_run(run)
            payload = {
                "valid": report.valid,
                "status": run.candidate.result.status,
                "errors": [error.model_dump(mode="json") for error in report.errors],
                "result": run.candidate.result.model_dump(mode="json"),
            }
            print(json.dumps(payload, indent=2))
            return 0 if report.valid else 1
        if args.command == "summarize":
            run = load_run(args.run_file)
            report = validate_run(run)
            result = run.candidate.result
            count_text = "no count" if result.count is None else f"{result.count} people"
            place_text = result.place_name or "no place"
            print(f"{result.status}: {count_text} at {place_text}")
            print(f"validation: {'valid' if report.valid else 'invalid'}")
            print(f"reason: {result.reason}")
            if report.errors:
                for error in report.errors:
                    print(f"- {error.code}: {error.message}")
            return 0 if report.valid else 1
        if args.command == "investigate-api":
            task = load_task(args.task_file)
            result = run_agent_investigation(task)
            print(result.model_dump_json(indent=2))
            return 0
        else:
            parser.error(f"unknown command: {args.command}")
    except MissingAPIKeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ObservationValidationException as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1

    return 0


def console() -> int:
    return main()
