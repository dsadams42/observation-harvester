from __future__ import annotations

from pathlib import Path

from pdt_observer.models import (
    InvestigationRun,
    ResultStatus,
    SourceOutcome,
    StopReason,
    WorkQuota,
    WorkStatus,
)
from pdt_observer.workflow import (
    claim_work_item,
    complete_work_item,
    create_batch,
    export_review_items,
    get_work_status,
    ingest_review,
    list_review_items,
    list_work_items,
    load_work_item_by_id,
    record_run,
    record_source_outcome,
    save_work_item,
)


def test_create_batch_writes_public_venue_work_items(tmp_path: Path) -> None:
    batch = create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
    )

    items = list_work_items(tmp_path)

    assert batch.batch_id == "batch-test"
    assert len(items) == 5
    assert all(item.status == WorkStatus.OPEN for item in items)
    assert all(item.quota.target_accepted_count == 5 for item in items)


def test_create_batch_applies_custom_quota(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
        quota=WorkQuota(target_accepted_count=2, max_sources_examined=7),
    )

    items = list_work_items(tmp_path)

    assert all(item.quota.target_accepted_count == 2 for item in items)
    assert all(item.quota.max_sources_examined == 7 for item in items)


def test_claim_work_item_marks_one_profile_item(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
    )

    claimed = claim_work_item(
        tmp_path,
        profile_id="restaurants_bars",
        claimed_by="codex-restaurants",
    )

    assert claimed is not None
    assert claimed.status == WorkStatus.CLAIMED
    assert claimed.claimed_by == "codex-restaurants"
    assert claimed.progress.started_at is not None
    assert claimed.progress.last_activity_at is not None
    assert claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="again") is None


def test_ingest_review_and_export_jsonl(tmp_path: Path) -> None:
    item = ingest_review(Path("examples/milltown_codex_run.json"), root=tmp_path)

    assert item.status == ResultStatus.ACCEPTED
    assert item.validation_valid

    items = list_review_items(tmp_path, status=ResultStatus.ACCEPTED)
    exported = export_review_items(tmp_path, status=ResultStatus.ACCEPTED, output_format="jsonl")

    assert len(items) == 1
    assert "Blue Lantern" in exported


def test_record_source_outcomes_increment_counters(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
    )
    claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")

    record_source_outcome(
        tmp_path,
        work_item_id="batch-test-restaurants_bars",
        outcome=SourceOutcome.EXAMINED,
    )
    record_source_outcome(
        tmp_path,
        work_item_id="batch-test-restaurants_bars",
        outcome=SourceOutcome.EMPTY,
    )
    report = record_source_outcome(
        tmp_path,
        work_item_id="batch-test-restaurants_bars",
        outcome=SourceOutcome.FAILED,
    )

    assert report.progress.sources_examined == 3
    assert report.progress.empty_sources == 1
    assert report.progress.failed_sources == 1
    assert report.should_continue


def test_record_run_ingests_review_and_stops_when_target_met(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
        quota=WorkQuota(target_accepted_count=1),
    )
    claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")

    report = record_run(
        tmp_path,
        work_item_id="batch-test-restaurants_bars",
        run_file=Path("examples/milltown_codex_run.json"),
    )

    assert report.progress.accepted_count == 1
    assert report.progress.sources_examined == 1
    assert report.progress.run_files == ("examples\\milltown_codex_run.json",)
    assert report.status == WorkStatus.COMPLETED
    assert report.stop_reason == StopReason.TARGET_MET
    assert not report.should_continue
    assert len(list_review_items(tmp_path, status=ResultStatus.ACCEPTED)) == 1


def test_review_limit_stops_after_invalid_run(tmp_path: Path) -> None:
    bad_run = tmp_path / "bad-run.json"
    run = InvestigationRun.model_validate_json(
        Path("examples/milltown_codex_run.json").read_text(encoding="utf-8")
    )
    assert run.candidate.result.evidence is not None
    bad_result = run.candidate.result.model_copy(
        deep=True,
        update={
            "evidence": run.candidate.result.evidence.model_copy(
                update={"supporting_quote": "Officials said 17 people were somewhere else."}
            )
        },
    )
    bad_candidate = run.candidate.model_copy(update={"result": bad_result})
    bad_run.write_text(
        run.model_copy(update={"candidate": bad_candidate}).model_dump_json(indent=2),
        encoding="utf-8",
    )
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
        quota=WorkQuota(max_review_count=1),
    )
    claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")

    report = record_run(
        tmp_path,
        work_item_id="batch-test-restaurants_bars",
        run_file=bad_run,
    )

    assert report.progress.review_count == 1
    assert report.status == WorkStatus.COMPLETED
    assert report.stop_reason == StopReason.REVIEW_LIMIT_REACHED


def test_source_limit_stops_work_item(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
        quota=WorkQuota(max_sources_examined=1),
    )
    claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")

    report = record_source_outcome(
        tmp_path,
        work_item_id="batch-test-restaurants_bars",
        outcome=SourceOutcome.EXAMINED,
    )

    assert report.status == WorkStatus.COMPLETED
    assert report.stop_reason == StopReason.SOURCE_LIMIT_REACHED


def test_failed_and_empty_limits_stop_work_items(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
        quota=WorkQuota(max_failed_sources=1, max_empty_sources=1),
    )
    claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")

    failed_report = record_source_outcome(
        tmp_path,
        work_item_id="batch-test-restaurants_bars",
        outcome=SourceOutcome.FAILED,
    )

    assert failed_report.stop_reason == StopReason.FAILED_SOURCE_LIMIT_REACHED

    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test-2",
        quota=WorkQuota(max_failed_sources=5, max_empty_sources=1),
    )
    claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")
    empty_report = record_source_outcome(
        tmp_path,
        work_item_id="batch-test-2-restaurants_bars",
        outcome=SourceOutcome.EMPTY,
    )

    assert empty_report.stop_reason == StopReason.EMPTY_SOURCE_LIMIT_REACHED


def test_runtime_limit_and_manual_completion(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
        quota=WorkQuota(max_runtime_minutes=0),
    )
    claimed = claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")

    assert claimed is not None
    report = get_work_status(tmp_path, "batch-test-restaurants_bars")
    assert report.status == WorkStatus.COMPLETED
    assert report.stop_reason == StopReason.RUNTIME_LIMIT_REACHED

    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test-2",
    )
    claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")
    manual = complete_work_item(tmp_path, "batch-test-2-restaurants_bars")

    assert manual.status == WorkStatus.COMPLETED
    assert manual.stop_reason == StopReason.MANUAL_COMPLETE


def test_completed_work_item_rejects_progress(tmp_path: Path) -> None:
    create_batch(
        root=tmp_path,
        locality="Milltown",
        country="US",
        profile_set_name="public_venues",
        batch_id="batch-test",
        quota=WorkQuota(target_accepted_count=1),
    )
    claim_work_item(tmp_path, profile_id="restaurants_bars", claimed_by="codex")
    record_run(
        tmp_path,
        work_item_id="batch-test-restaurants_bars",
        run_file=Path("examples/milltown_codex_run.json"),
    )

    item = load_work_item_by_id(tmp_path, "batch-test-restaurants_bars")
    save_work_item(tmp_path, item)

    try:
        record_source_outcome(
            tmp_path,
            work_item_id="batch-test-restaurants_bars",
            outcome=SourceOutcome.EXAMINED,
        )
    except ValueError as exc:
        assert "already completed" in str(exc)
    else:
        raise AssertionError("expected completed work item to reject progress")
