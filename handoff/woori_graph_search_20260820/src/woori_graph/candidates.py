"""Create human-review candidate dictionaries from raw SVO JSONL."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from .ids import stable_id


def build_candidate_dictionaries(
    raw_records: Iterable[dict[str, Any]], *, sample_limit: int = 5
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group exact surface forms only; semantic clustering remains human work."""

    entity_occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    relation_occurrences: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in raw_records:
        source = {
            "semantic_unit_id": record["semantic_unit_id"],
            "document_id": record["document_id"],
            "source_ref": record["source_ref"],
        }
        for relation in record.get("relations", []):
            for endpoint in (relation["subject"], relation["object"]):
                entity_occurrences[endpoint].append(source)
            relation_occurrences[relation["predicate"]].append(source)

    entity_candidates = [
        {
            "entity_id": stable_id("entity_candidate", name),
            "canonical_name": name,
            "aliases": [],
            "sample_source_refs": _unique_samples(occurrences, sample_limit),
            "mention_count": len(occurrences),
            "clustering_rationale": "원시 표현 완전 일치 기반 자동 후보. 사람 검토 전에는 최종 사전이 아님.",
        }
        for name, occurrences in sorted(entity_occurrences.items())
    ]
    relation_candidates = [
        {
            "relation_type_id": stable_id("relation_type_candidate", name),
            "canonical_name": name,
            "polarity": None,
            "aliases": [],
            "sample_source_refs": _unique_samples(occurrences, sample_limit),
            "mention_count": len(occurrences),
            "clustering_rationale": "원시 술어 완전 일치 기반 자동 후보. 긍정/부정 및 군집은 사람 검토 전에는 확정하지 않음.",
        }
        for name, occurrences in sorted(relation_occurrences.items())
    ]
    return entity_candidates, relation_candidates


def _unique_samples(occurrences: list[dict[str, Any]], sample_limit: int) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for occurrence in occurrences:
        key = occurrence["semantic_unit_id"]
        if key in seen:
            continue
        seen.add(key)
        samples.append(occurrence)
        if len(samples) == sample_limit:
            break
    return samples
