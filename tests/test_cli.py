from __future__ import annotations

import json

from pdt_observer.cli import main
from pdt_observer.models import InvestigationResult


def test_cli_demo_output_is_valid_json(capsys) -> None:
    exit_code = main(["demo"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    result = InvestigationResult.model_validate(payload)

    assert exit_code == 0
    assert result.status == "accepted"
    assert result.count == 17
    assert captured.err == ""
