"""Force unseen predicates into an existing closed relation dictionary."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..extraction import CompletionClient
from ..prompting import load_prompt_asset


def propose_forced_relation_overrides(
    predicates: Sequence[str],
    relation_dictionary: Sequence[dict[str, Any]],
    client: CompletionClient,
    *,
    batch_size: int = 40,
    workers: int = 4,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Return raw predicate -> released relation_type_id mappings.

    This function deliberately has no free-form fallback. Missing or invalid
    LLM results remain errors and are rejected later by graph mapping.
    """

    if batch_size < 1 or workers < 1:
        raise ValueError("batch_size and workers must be at least 1")
    ordered_predicates = sorted({item.strip() for item in predicates if item.strip()})
    if not ordered_predicates:
        return {}, []
    relation_by_id = {
        item["relation_type_id"]: item for item in relation_dictionary
    }
    if not relation_by_id:
        raise ValueError("relation_dictionary must not be empty")
    dictionary_payload = [
        {
            "relation_type_id": item["relation_type_id"],
            "canonical_name": item["canonical_name"],
            "polarity": item.get("polarity"),
        }
        for item in sorted(
            relation_dictionary,
            key=lambda value: (value["canonical_name"], value["relation_type_id"]),
        )
    ]
    prompt = load_prompt_asset("relation_classify.ko.md")
    batches = [
        ordered_predicates[start : start + batch_size]
        for start in range(0, len(ordered_predicates), batch_size)
    ]
    mappings: dict[str, str] = {}
    errors: list[dict[str, Any]] = []

    def map_batch(batch_index: int, batch: list[str]):
        payload = {
            "relation_dictionary": dictionary_payload,
            "raw_predicates": batch,
        }
        response = client.complete(
            f"{prompt}\n\n입력:\n{json.dumps(payload, ensure_ascii=False)}"
        )
        value = _parse_json_object(response)
        items = value.get("items")
        if not isinstance(items, list):
            raise ValueError("response must contain an items array")
        batch_mapping: dict[str, str] = {}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            predicate = item.get("raw_predicate")
            relation_type_id = item.get("relation_type_id")
            if predicate in batch and relation_type_id in relation_by_id:
                if predicate in batch_mapping:
                    raise ValueError(f"duplicate mapping for predicate {predicate!r}")
                batch_mapping[predicate] = relation_type_id
        missing = sorted(set(batch) - set(batch_mapping))
        if missing:
            raise ValueError(f"response omitted {len(missing)} predicates")
        return batch_index, batch_mapping

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(map_batch, batch_index, batch): (batch_index, batch)
            for batch_index, batch in enumerate(batches)
        }
        for future in as_completed(futures):
            batch_index, batch = futures[future]
            try:
                _, batch_mapping = future.result()
                mappings.update(batch_mapping)
            except Exception as exc:
                errors.append(
                    {
                        "batch_index": batch_index,
                        "raw_predicates": batch,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
    return mappings, sorted(errors, key=lambda item: item["batch_index"])


def _parse_json_object(response: str) -> dict[str, Any]:
    candidate = response.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", maxsplit=1)[-1]
        if candidate.rstrip().endswith("```"):
            candidate = candidate.rstrip()[:-3]
    value = json.loads(candidate)
    if not isinstance(value, dict):
        raise ValueError("response must be a JSON object")
    return value
