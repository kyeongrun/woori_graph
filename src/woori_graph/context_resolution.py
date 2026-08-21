"""Resolve segmented source fragments into standalone extraction text."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Any

from .extraction import CompletionClient
from .models import SemanticUnit
from .prompting import load_prompt_asset


DEFAULT_CONTEXT_RESOLUTION_PROMPT = load_prompt_asset("context_resolve.ko.md")
_RESOLUTION_TYPES = {"COPIED", "CONTEXT_INHERITED"}


def resolve_unit_context(
    unit: SemanticUnit,
    client: CompletionClient,
    *,
    prompt_template: str = DEFAULT_CONTEXT_RESOLUTION_PROMPT,
) -> SemanticUnit:
    """Return the same source unit with an auditable standalone resolved text."""

    if unit.unit_kind != "terminal_item":
        return replace(
            unit,
            resolved_text=unit.unit_text,
            resolution_type="COPIED",
        )

    payload = {
        "document_title": unit.document_title,
        "source_ref": unit.source_ref.to_dict(),
        "context_text": unit.context_text,
        "governing_text": unit.governing_text,
        "unit_text": unit.unit_text,
        "unit_kind": unit.unit_kind,
    }
    response = client.complete(
        f"{prompt_template}\n\n입력:\n{json.dumps(payload, ensure_ascii=False)}"
    )
    parsed = _parse_resolution(response)
    resolved_text = parsed["resolved_text"]
    resolution_type = (
        "COPIED"
        if resolved_text.strip() == unit.unit_text.strip()
        else "CONTEXT_INHERITED"
    )
    return replace(
        unit,
        resolved_text=resolved_text,
        resolution_type=resolution_type,
    )


def resolve_units(
    units: Sequence[SemanticUnit],
    client: CompletionClient,
    *,
    workers: int = 1,
    prompt_template: str = DEFAULT_CONTEXT_RESOLUTION_PROMPT,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve units concurrently while preserving their source order."""

    if workers < 1:
        raise ValueError("workers must be at least 1")
    outputs: list[dict[str, Any] | None] = [None] * len(units)
    errors: list[dict[str, Any]] = []

    def process(index: int, unit: SemanticUnit) -> tuple[int, dict[str, Any]]:
        resolved = resolve_unit_context(
            unit,
            client,
            prompt_template=prompt_template,
        )
        return index, resolved.to_dict()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(process, index, unit): (index, unit)
            for index, unit in enumerate(units)
        }
        for future in as_completed(futures):
            index, unit = futures[future]
            try:
                output_index, record = future.result()
                outputs[output_index] = record
            except Exception as exc:
                errors.append(
                    {
                        "semantic_unit_id": unit.semantic_unit_id,
                        "document_id": unit.document_id,
                        "source_ref": unit.source_ref.to_dict(),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
    return [record for record in outputs if record is not None], errors


def audit_context_resolution(
    source_records: Sequence[Mapping[str, Any]],
    resolved_records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Verify full coverage and immutable source evidence after resolution."""

    source_ids = [str(record["semantic_unit_id"]) for record in source_records]
    resolved_ids = [str(record["semantic_unit_id"]) for record in resolved_records]
    resolved_by_id = {
        str(record["semantic_unit_id"]): record for record in resolved_records
    }
    source_fields = (
        "document_id",
        "document_title",
        "source_path",
        "source_ref",
        "context_text",
        "governing_text",
        "unit_text",
        "unit_kind",
    )
    source_mismatch_ids: list[str] = []
    invalid_resolution_ids: list[str] = []
    copied_text_mismatch_ids: list[str] = []
    resolution_counts = {"COPIED": 0, "CONTEXT_INHERITED": 0, "INVALID": 0}

    for source in source_records:
        identifier = str(source["semantic_unit_id"])
        resolved = resolved_by_id.get(identifier)
        if resolved is None:
            continue
        if any(source.get(field, "") != resolved.get(field, "") for field in source_fields):
            source_mismatch_ids.append(identifier)
        resolution_type = resolved.get("resolution_type")
        resolved_text = resolved.get("resolved_text")
        if (
            resolution_type not in _RESOLUTION_TYPES
            or not isinstance(resolved_text, str)
            or not resolved_text.strip()
        ):
            invalid_resolution_ids.append(identifier)
            resolution_counts["INVALID"] += 1
            continue
        resolution_counts[str(resolution_type)] += 1
        if resolution_type == "COPIED" and resolved_text.strip() != str(source["unit_text"]).strip():
            copied_text_mismatch_ids.append(identifier)

    missing_ids = sorted(set(source_ids) - set(resolved_ids))
    extra_ids = sorted(set(resolved_ids) - set(source_ids))
    checks = {
        "source_semantic_unit_ids_unique": len(source_ids) == len(set(source_ids)),
        "resolved_semantic_unit_ids_unique": len(resolved_ids) == len(set(resolved_ids)),
        "coverage_exact": not missing_ids and not extra_ids,
        "source_order_matches": source_ids == resolved_ids,
        "source_fields_preserved": not source_mismatch_ids,
        "all_units_resolved": not invalid_resolution_ids,
        "copied_units_are_verbatim": not copied_text_mismatch_ids,
    }
    return {
        "passed": all(checks.values()),
        "counts": {
            "source_units": len(source_records),
            "resolved_units": len(resolved_records),
            "resolution_types": resolution_counts,
            "missing_units": len(missing_ids),
            "extra_units": len(extra_ids),
        },
        "checks": checks,
        "details": {
            "missing_semantic_unit_ids": missing_ids,
            "extra_semantic_unit_ids": extra_ids,
            "source_mismatch_ids": source_mismatch_ids,
            "invalid_resolution_ids": invalid_resolution_ids,
            "copied_text_mismatch_ids": copied_text_mismatch_ids,
        },
    }


def normalize_context_resolution_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Derive resolution provenance from source and resolved text."""

    normalized: list[dict[str, Any]] = []
    for record in records:
        resolved_text = record.get("resolved_text")
        unit_text = record.get("unit_text")
        if not isinstance(resolved_text, str) or not resolved_text.strip():
            raise ValueError(
                f"semantic unit {record.get('semantic_unit_id')!r} has empty resolved_text"
            )
        if not isinstance(unit_text, str) or not unit_text.strip():
            raise ValueError(
                f"semantic unit {record.get('semantic_unit_id')!r} has empty unit_text"
            )
        item = dict(record)
        item["resolution_type"] = (
            "COPIED"
            if resolved_text.strip() == unit_text.strip()
            else "CONTEXT_INHERITED"
        )
        normalized.append(item)
    return normalized


def _parse_resolution(response_text: str) -> dict[str, str]:
    try:
        value = json.loads(response_text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("context resolution response must be valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("context resolution response must be a JSON object")
    resolved_text = value.get("resolved_text")
    resolution_type = value.get("resolution_type")
    if not isinstance(resolved_text, str) or not resolved_text.strip():
        raise ValueError("resolved_text must be a non-empty string")
    if resolution_type not in _RESOLUTION_TYPES:
        raise ValueError(
            "resolution_type must be COPIED or CONTEXT_INHERITED"
        )
    return {
        "resolved_text": resolved_text.strip(),
        "resolution_type": resolution_type,
    }
