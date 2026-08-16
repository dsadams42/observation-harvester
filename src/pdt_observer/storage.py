from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path


def write_json_file(
    path: Path,
    payload: object,
    *,
    indent: int | None = 2,
    sort_keys: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=indent, sort_keys=sort_keys)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as handle:
        handle.write(text)
        temp_path = Path(handle.name)
    for attempt in range(5):
        try:
            temp_path.replace(path)
            break
        except PermissionError:
            if attempt == 4:
                temp_path.unlink(missing_ok=True)
                raise
            time.sleep(0.02 * (attempt + 1))
