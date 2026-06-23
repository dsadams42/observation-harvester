from __future__ import annotations

import pytest
from pydantic import ValidationError

from pdt_observer.models import InvestigationResult, ResultStatus


def test_accepted_result_cannot_omit_required_fields() -> None:
    with pytest.raises(ValidationError):
        InvestigationResult(status=ResultStatus.ACCEPTED, reason="Incomplete accepted result.")
