"""Small, dependency-free JSONL helpers with safe overwrite behaviour."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL record at {path}:{line_number} must be an object")
            yield value


def write_jsonl(
    path: Path,
    records: Iterable[Mapping[str, Any]],
    *,
    overwrite: bool = False,
    leading_keys: Sequence[str] = (),
) -> int:
    """Write UTF-8 JSONL and return the record count.

    Existing files are protected unless the caller explicitly opts in to
    overwriting them. This matters because review JSONL is manually curated.
    """

    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            if leading_keys:
                ordered_record = {
                    key: record[key] for key in leading_keys if key in record
                }
                ordered_record.update(
                    (key, record[key]) for key in sorted(record) if key not in ordered_record
                )
                handle.write(json.dumps(ordered_record, ensure_ascii=False))
            else:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            count += 1
    return count


def append_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    """Append generated batch records to an existing UTF-8 JSONL file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            count += 1
    return count
