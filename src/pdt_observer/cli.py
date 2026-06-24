from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from pdt_observer.agent import load_task, run_agent_investigation, run_offline_demo
from pdt_observer.config import MissingAPIKeyError
from pdt_observer.leads import (
    leads_to_json,
    load_leads,
    render_lead_harvest_prompt,
    summarize_leads,
)
from pdt_observer.models import (
    InvestigationRun,
    ResultStatus,
    SourceOutcome,
    WorkQuota,
    WorkStatus,
)
from pdt_observer.profiles import get_builtin_profile
from pdt_observer.prompting import render_work_prompt
from pdt_observer.validation import ObservationValidationException, validate_run
from pdt_observer.web import DirectFetchError, DirectWebFetcher
from pdt_observer.workflow import (
    claim_work_item,
    complete_work_item,
    create_batch,
    export_review_items,
    get_work_status,
    ingest_review,
    list_review_items,
    list_work_items,
    record_run,
    record_source_outcome,
    write_model,
)


def load_run(path: Path) -> InvestigationRun:
    return InvestigationRun.model_validate_json(path.read_text(encoding="utf-8"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdt-observer",
        description="Run a bounded PDT observation-harvesting investigation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("demo", help="Run the deterministic offline Milltown demo.")

    batch = subparsers.add_parser("batch", help="Manage Codex-operated harvest batches.")
    batch_subparsers = batch.add_subparsers(dest="batch_command", required=True)
    batch_create = batch_subparsers.add_parser("create", help="Create profile work items.")
    batch_create.add_argument("--locality", required=True)
    batch_create.add_argument("--country", required=True)
    batch_create.add_argument("--profiles", default="public_venues")
    batch_create.add_argument("--batch-id")
    batch_create.add_argument("--source-hint", action="append", default=[])
    batch_create.add_argument("--workspace", type=Path, default=Path("."))
    batch_create.add_argument("--target-accepted", type=int, default=5)
    batch_create.add_argument("--max-review", type=int, default=10)
    batch_create.add_argument("--max-sources", type=int, default=40)
    batch_create.add_argument("--max-failed-sources", type=int, default=20)
    batch_create.add_argument("--max-empty-sources", type=int, default=15)
    batch_create.add_argument("--max-runtime-minutes", type=int, default=60)

    work = subparsers.add_parser("work", help="List or claim Codex work items.")
    work_subparsers = work.add_subparsers(dest="work_command", required=True)
    work_list = work_subparsers.add_parser("list", help="List work items.")
    work_list.add_argument("--workspace", type=Path, default=Path("."))
    work_list.add_argument("--status", choices=[status.value for status in WorkStatus])
    work_list.add_argument("--profile")
    work_list.add_argument("--locality")
    work_list.add_argument("--country")
    work_claim = work_subparsers.add_parser("claim", help="Claim the next open work item.")
    work_claim.add_argument("--workspace", type=Path, default=Path("."))
    work_claim.add_argument("--profile")
    work_claim.add_argument("--locality")
    work_claim.add_argument("--country")
    work_claim.add_argument("--work-item-id")
    work_claim.add_argument("--claimed-by", default="codex")
    work_status = work_subparsers.add_parser("status", help="Show quota progress for a work item.")
    work_status.add_argument("--workspace", type=Path, default=Path("."))
    work_status.add_argument("--work-item-id", required=True)
    work_prompt = work_subparsers.add_parser(
        "prompt",
        help="Render a profile-specific Codex prompt for a work item.",
    )
    work_prompt.add_argument("--workspace", type=Path, default=Path("."))
    work_prompt.add_argument("--work-item-id", required=True)
    work_record_source = work_subparsers.add_parser(
        "record-source",
        help="Record one source inspection outcome.",
    )
    work_record_source.add_argument("--workspace", type=Path, default=Path("."))
    work_record_source.add_argument("--work-item-id", required=True)
    work_record_source.add_argument(
        "--outcome",
        required=True,
        choices=[outcome.value for outcome in SourceOutcome],
    )
    work_record_run = work_subparsers.add_parser(
        "record-run",
        help="Validate, ingest, and count one InvestigationRun file.",
    )
    work_record_run.add_argument("--workspace", type=Path, default=Path("."))
    work_record_run.add_argument("--work-item-id", required=True)
    work_record_run.add_argument("--run-file", type=Path, required=True)
    work_complete = work_subparsers.add_parser("complete", help="Manually complete a work item.")
    work_complete.add_argument("--workspace", type=Path, default=Path("."))
    work_complete.add_argument("--work-item-id", required=True)

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

    review = subparsers.add_parser("review", help="Ingest and list review queue items.")
    review_subparsers = review.add_subparsers(dest="review_command", required=True)
    review_ingest = review_subparsers.add_parser("ingest", help="Ingest a validated run file.")
    review_ingest.add_argument("run_file", type=Path)
    review_ingest.add_argument("--workspace", type=Path, default=Path("."))
    review_list = review_subparsers.add_parser("list", help="List review queue items.")
    review_list.add_argument("--workspace", type=Path, default=Path("."))
    review_list.add_argument("--status", choices=[status.value for status in ResultStatus])

    export = subparsers.add_parser("export", help="Export review queue items.")
    export.add_argument("--workspace", type=Path, default=Path("."))
    export.add_argument(
        "--status",
        required=True,
        choices=[status.value for status in ResultStatus],
    )
    export.add_argument("--format", default="jsonl", choices=["jsonl"])

    source = subparsers.add_parser("source", help="Fetch direct URLs supplied by Codex or a user.")
    source_subparsers = source.add_subparsers(dest="source_command", required=True)
    source_fetch = source_subparsers.add_parser("fetch", help="Fetch one direct public URL.")
    source_fetch.add_argument("url")
    source_fetch.add_argument("--output", type=Path)
    source_fetch.add_argument("--max-bytes", type=int, default=1_000_000)
    source_fetch.add_argument("--timeout-seconds", type=float, default=10)

    harvest = subparsers.add_parser("harvest", help="Prepare broad Codex lead-harvest prompts.")
    harvest_subparsers = harvest.add_subparsers(dest="harvest_command", required=True)
    harvest_prepare = harvest_subparsers.add_parser(
        "prepare",
        help="Render a broad country/profile lead-harvest prompt.",
    )
    harvest_prepare.add_argument("--country", required=True)
    harvest_prepare.add_argument("--profiles", default="commercial_business")
    harvest_prepare.add_argument("--target", type=int, default=20)
    harvest_prepare.add_argument("--locality")
    harvest_prepare.add_argument("--output", type=Path)

    leads = subparsers.add_parser("leads", help="Validate and summarize broad lead JSON arrays.")
    leads_subparsers = leads.add_subparsers(dest="leads_command", required=True)
    leads_validate = leads_subparsers.add_parser("validate", help="Validate a lead JSON array.")
    leads_validate.add_argument("lead_file", type=Path)
    leads_validate.add_argument("--pretty", action="store_true")
    leads_summarize = leads_subparsers.add_parser("summarize", help="Summarize a lead JSON array.")
    leads_summarize.add_argument("lead_file", type=Path)

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
        if args.command == "batch" and args.batch_command == "create":
            quota = WorkQuota(
                target_accepted_count=args.target_accepted,
                max_review_count=args.max_review,
                max_sources_examined=args.max_sources,
                max_failed_sources=args.max_failed_sources,
                max_empty_sources=args.max_empty_sources,
                max_runtime_minutes=args.max_runtime_minutes,
            )
            batch = create_batch(
                root=args.workspace,
                locality=args.locality,
                country=args.country,
                profile_set_name=args.profiles,
                batch_id=args.batch_id,
                source_hints=tuple(args.source_hint),
                quota=quota,
            )
            print(batch.model_dump_json(indent=2))
            return 0
        if args.command == "work" and args.work_command == "list":
            status = None if args.status is None else WorkStatus(args.status)
            items = list_work_items(
                args.workspace,
                status=status,
                profile_id=args.profile,
                locality=args.locality,
                country=args.country,
            )
            print(json.dumps([item.model_dump(mode="json") for item in items], indent=2))
            return 0
        if args.command == "work" and args.work_command == "claim":
            claimed_item = claim_work_item(
                args.workspace,
                profile_id=args.profile,
                locality=args.locality,
                country=args.country,
                work_item_id=args.work_item_id,
                claimed_by=args.claimed_by,
            )
            if claimed_item is None:
                print("No open work item matched the requested claim filters.", file=sys.stderr)
                return 1
            print(claimed_item.model_dump_json(indent=2))
            return 0
        if args.command == "work" and args.work_command == "status":
            work_report = get_work_status(args.workspace, args.work_item_id)
            print(work_report.model_dump_json(indent=2))
            return 0
        if args.command == "work" and args.work_command == "prompt":
            work_report = get_work_status(args.workspace, args.work_item_id)
            matching_items = list_work_items(args.workspace)
            item = next(
                (
                    candidate
                    for candidate in matching_items
                    if candidate.work_item_id == args.work_item_id
                ),
                None,
            )
            if item is None:
                raise ValueError(f"work item not found: {args.work_item_id}")
            profile = get_builtin_profile(item.profile_id)
            print(render_work_prompt(item=item, profile=profile, status=work_report))
            return 0
        if args.command == "work" and args.work_command == "record-source":
            work_report = record_source_outcome(
                args.workspace,
                work_item_id=args.work_item_id,
                outcome=SourceOutcome(args.outcome),
            )
            print(work_report.model_dump_json(indent=2))
            return 0
        if args.command == "work" and args.work_command == "record-run":
            work_report = record_run(
                args.workspace,
                work_item_id=args.work_item_id,
                run_file=args.run_file,
            )
            print(work_report.model_dump_json(indent=2))
            return 0
        if args.command == "work" and args.work_command == "complete":
            work_report = complete_work_item(args.workspace, args.work_item_id)
            print(work_report.model_dump_json(indent=2))
            return 0
        if args.command == "validate":
            run = load_run(args.run_file)
            validation_report = validate_run(run)
            payload = {
                "valid": validation_report.valid,
                "status": run.candidate.result.status,
                "errors": [error.model_dump(mode="json") for error in validation_report.errors],
                "result": run.candidate.result.model_dump(mode="json"),
            }
            print(json.dumps(payload, indent=2))
            return 0 if validation_report.valid else 1
        if args.command == "summarize":
            run = load_run(args.run_file)
            validation_report = validate_run(run)
            result = run.candidate.result
            count_text = "no count" if result.count is None else f"{result.count} people"
            place_text = result.place_name or "no place"
            print(f"{result.status}: {count_text} at {place_text}")
            print(f"validation: {'valid' if validation_report.valid else 'invalid'}")
            print(f"reason: {result.reason}")
            if validation_report.errors:
                for error in validation_report.errors:
                    print(f"- {error.code}: {error.message}")
            return 0 if validation_report.valid else 1
        if args.command == "review" and args.review_command == "ingest":
            review_queue_item = ingest_review(args.run_file, root=args.workspace)
            print(review_queue_item.model_dump_json(indent=2))
            return 0
        if args.command == "review" and args.review_command == "list":
            review_status = None if args.status is None else ResultStatus(args.status)
            review_items = list_review_items(args.workspace, status=review_status)
            print(json.dumps([item.model_dump(mode="json") for item in review_items], indent=2))
            return 0
        if args.command == "export":
            print(
                export_review_items(
                    args.workspace,
                    status=ResultStatus(args.status),
                    output_format=args.format,
                ),
                end="",
            )
            return 0
        if args.command == "source" and args.source_command == "fetch":
            fetcher = DirectWebFetcher(
                max_bytes=args.max_bytes,
                timeout_seconds=args.timeout_seconds,
            )
            fetch_result = fetcher.fetch(args.url)
            if args.output is not None:
                write_model(args.output, fetch_result)
            print(fetch_result.model_dump_json(indent=2))
            return 0
        if args.command == "harvest" and args.harvest_command == "prepare":
            prompt = render_lead_harvest_prompt(
                country=args.country,
                profile_set_name=args.profiles,
                target=args.target,
                locality=args.locality,
            )
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(prompt, encoding="utf-8")
            print(prompt, end="" if prompt.endswith("\n") else "\n")
            return 0
        if args.command == "leads" and args.leads_command == "validate":
            lead_items = load_leads(args.lead_file)
            if args.pretty:
                print(leads_to_json(lead_items))
            else:
                print(json.dumps({"valid": True, "lead_count": len(lead_items)}, indent=2))
            return 0
        if args.command == "leads" and args.leads_command == "summarize":
            print(json.dumps(summarize_leads(load_leads(args.lead_file)), indent=2))
            return 0
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
    except (DirectFetchError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return 0


def console() -> int:
    return main()
