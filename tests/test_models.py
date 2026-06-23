from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from pdt_observer.models import InvestigationResult, InvestigationRun, ResultStatus


def test_accepted_result_cannot_omit_required_fields() -> None:
    with pytest.raises(ValidationError):
        InvestigationResult(status=ResultStatus.ACCEPTED, reason="Incomplete accepted result.")


def test_codex_run_model_loads_example() -> None:
    run = InvestigationRun.model_validate_json(
        Path("examples/milltown_codex_run.json").read_text(encoding="utf-8")
    )

    assert run.task.locality == "Milltown"
    assert run.candidate.produced_by == "codex"
    assert run.candidate.result.count == 17
    assert run.source_bundle.documents
    assert run.source_bundle.places
