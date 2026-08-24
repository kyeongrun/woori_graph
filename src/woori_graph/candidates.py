"""Create human-review candidate dictionaries from raw SVO JSONL."""

from __future__ import annotations

from collections import Counter, defaultdict
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


def build_simple_surface_lists(
    raw_records: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build one JSONL-ready row per exact entity or relation surface form.

    The first semantic unit containing a surface form is retained as readable
    source evidence. Repeated mentions are counted instead of being emitted as
    duplicate list rows.
    """

    entities: dict[str, dict[str, Any]] = {}
    relations: dict[str, dict[str, Any]] = {}

    for record in raw_records:
        source_text = record.get("resolved_text") or record.get("unit_text", "")
        semantic_unit_id = record["semantic_unit_id"]
        source_metadata = {
            "source_text": source_text,
            "semantic_unit_id": semantic_unit_id,
            "document_title": record.get("document_title", ""),
            "source_ref": record.get("source_ref", {}),
        }
        for triple in record.get("relations", []):
            for role in ("subject", "object"):
                name = triple[role]
                item = entities.get(name)
                if item is None:
                    item = {
                        "name": name,
                        **source_metadata,
                        "roles": [],
                        "mention_count": 0,
                    }
                    entities[name] = item
                item["mention_count"] += 1
                if role not in item["roles"]:
                    item["roles"].append(role)

            predicate = triple["predicate"]
            relation = relations.get(predicate)
            if relation is None:
                relation = {
                    "name": predicate,
                    **source_metadata,
                    "mention_count": 0,
                }
                relations[predicate] = relation
            relation["mention_count"] += 1

    return (
        [entities[name] for name in sorted(entities)],
        [relations[name] for name in sorted(relations)],
    )


def audit_simple_surface_lists(
    raw_records: Iterable[dict[str, Any]],
    entity_candidates: list[dict[str, Any]],
    relation_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Verify exact coverage, counts, and evidence for source-bearing lists."""

    records = list(raw_records)
    expected_entities: dict[str, int] = defaultdict(int)
    expected_relations: dict[str, int] = defaultdict(int)
    records_by_id = {record["semantic_unit_id"]: record for record in records}
    for record in records:
        for triple in record.get("relations", []):
            expected_entities[triple["subject"]] += 1
            expected_entities[triple["object"]] += 1
            expected_relations[triple["predicate"]] += 1

    def inspect(
        candidates: list[dict[str, Any]], expected: dict[str, int], role: str
    ) -> tuple[list[str], list[str], list[str]]:
        names = [str(item.get("name", "")) for item in candidates]
        duplicate_names = sorted(
            name for name, count in Counter(names).items() if count > 1
        )
        count_mismatches: list[str] = []
        evidence_mismatches: list[str] = []
        for item in candidates:
            name = str(item.get("name", ""))
            if item.get("mention_count") != expected.get(name):
                count_mismatches.append(name)
            record = records_by_id.get(str(item.get("semantic_unit_id", "")))
            if record is None:
                evidence_mismatches.append(name)
                continue
            expected_text = record.get("resolved_text") or record.get("unit_text", "")
            triples = record.get("relations", [])
            present = (
                any(name in (triple["subject"], triple["object"]) for triple in triples)
                if role == "entity"
                else any(name == triple["predicate"] for triple in triples)
            )
            if item.get("source_text") != expected_text or not present:
                evidence_mismatches.append(name)
        return duplicate_names, count_mismatches, evidence_mismatches

    entity_duplicates, entity_count_mismatches, entity_evidence_mismatches = inspect(
        entity_candidates, expected_entities, "entity"
    )
    relation_duplicates, relation_count_mismatches, relation_evidence_mismatches = inspect(
        relation_candidates, expected_relations, "relation"
    )
    entity_names = {item.get("name") for item in entity_candidates}
    relation_names = {item.get("name") for item in relation_candidates}
    checks = {
        "entity_names_unique": not entity_duplicates,
        "relation_names_unique": not relation_duplicates,
        "entity_coverage_exact": entity_names == set(expected_entities),
        "relation_coverage_exact": relation_names == set(expected_relations),
        "entity_mention_counts_match": not entity_count_mismatches,
        "relation_mention_counts_match": not relation_count_mismatches,
        "entity_evidence_matches": not entity_evidence_mismatches,
        "relation_evidence_matches": not relation_evidence_mismatches,
    }
    return {
        "passed": all(checks.values()),
        "counts": {
            "raw_records": len(records),
            "entity_candidates": len(entity_candidates),
            "relation_candidates": len(relation_candidates),
        },
        "checks": checks,
        "details": {
            "entity_duplicate_names": entity_duplicates[:20],
            "relation_duplicate_names": relation_duplicates[:20],
            "entity_count_mismatches": entity_count_mismatches[:20],
            "relation_count_mismatches": relation_count_mismatches[:20],
            "entity_evidence_mismatches": entity_evidence_mismatches[:20],
            "relation_evidence_mismatches": relation_evidence_mismatches[:20],
        },
    }


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
