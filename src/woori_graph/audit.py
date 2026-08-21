"""Completion checks for generated v3 JSONL artifacts."""

from __future__ import annotations

import re
from typing import Any

from .ids import stable_id
from .entity_resolution import SELF_REFERENCES, resolve_entity_name
from .graph_mapping import build_entity_alias_index, build_relation_alias_index
from .entity_typing import ENTITY_TYPE_SET


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

_NUMERIC_CONSTRAINT_RE = re.compile(
    r"(?:\d[\d,]*(?:\.\d+)?\s*(?:억원|만원|천원|원|년|개월|일|시간|회|명|인|세|급|등급|촌|퍼센트|%)"
    r"\s*(?:이상|이하|초과|미만|이내|간|동안)?|\d+\s*분의\s*\d+)"
)
_LEGAL_QUALIFIER_RE = re.compile(
    r"(?:「[^」]+」|[가-힣A-Za-z0-9·ㆍ\s]+법)\s*(?:제\d+조(?:제\d+항)?)?\s*(?:에\s*)?(?:따른|따라)"
)
_ENUMERATION_RE = re.compile(r"(?:\s(?:및|또는|내지)\s|[ㆍ,])")
_GENERIC_ENDPOINTS = {
    "각 호",
    "각호",
    "사항",
    "내용",
    "대상",
    "업무",
    "행위",
    "것",
    "경우",
    "금액",
    "기간",
    "비율",
    "횟수",
    "범위",
}


def audit_raw_svo(
    units: list[dict[str, Any]],
    raw_records: list[dict[str, Any]],
    *,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Audit a standalone semantic-unit/raw-SVO run before dictionary building."""

    unit_ids = [record["semantic_unit_id"] for record in units]
    raw_ids = [record["semantic_unit_id"] for record in raw_records]
    units_by_id = {record["semantic_unit_id"]: record for record in units}
    raw_by_id = {record["semantic_unit_id"]: record for record in raw_records}
    missing_ids = sorted(set(unit_ids) - set(raw_ids))
    extra_ids = sorted(set(raw_ids) - set(unit_ids))
    source_fields = (
        "document_id",
        "document_title",
        "source_path",
        "source_ref",
        "context_text",
        "unit_text",
        "unit_kind",
    )
    source_mismatches: list[str] = []
    invalid_mentions: list[str] = []
    incomplete_relations: list[str] = []
    duplicate_triples: list[str] = []
    constraint_samples: list[dict[str, str]] = []
    legal_qualifier_samples: list[dict[str, str]] = []
    enumeration_samples: list[dict[str, str]] = []
    generic_endpoint_samples: list[dict[str, str]] = []
    context_only_samples: list[dict[str, str]] = []
    warning_counts = {
        "numeric_constraint_endpoints": 0,
        "legal_qualifier_endpoints": 0,
        "enumerations": 0,
        "generic_endpoints": 0,
        "context_only_terminal_relations": 0,
    }
    relation_count = 0
    empty_record_count = 0

    def add_warning(
        kind: str,
        bucket: list[dict[str, str]],
        record_id: str,
        field: str,
        value: str,
    ) -> None:
        warning_counts[kind] += 1
        if len(bucket) < sample_limit:
            bucket.append({"semantic_unit_id": record_id, "field": field, "value": value})

    for record_id in unit_ids:
        if record_id not in raw_by_id:
            continue
        unit = units_by_id[record_id]
        record = raw_by_id[record_id]
        if any(record.get(field) != unit.get(field) for field in source_fields):
            source_mismatches.append(record_id)
        relations = record.get("relations")
        if not isinstance(relations, list):
            incomplete_relations.append(record_id)
            continue
        if not relations:
            empty_record_count += 1
        seen: set[tuple[str, str, str]] = set()
        for relation in relations:
            relation_count += 1
            if not isinstance(relation, dict) or any(
                not isinstance(relation.get(field), str) or not relation[field].strip()
                for field in ("subject", "predicate", "object", "relation_mention_id")
            ):
                incomplete_relations.append(record_id)
                continue
            triple = (relation["subject"], relation["predicate"], relation["object"])
            if triple in seen:
                duplicate_triples.append(relation["relation_mention_id"])
            seen.add(triple)
            expected_id = stable_id("raw_svo_mention", record_id, *triple)
            if relation["relation_mention_id"] != expected_id:
                invalid_mentions.append(relation["relation_mention_id"])
            for field in ("subject", "object"):
                value = relation[field]
                if _NUMERIC_CONSTRAINT_RE.search(value):
                    add_warning("numeric_constraint_endpoints", constraint_samples, record_id, field, value)
                if _LEGAL_QUALIFIER_RE.search(value):
                    add_warning("legal_qualifier_endpoints", legal_qualifier_samples, record_id, field, value)
                if _ENUMERATION_RE.search(value):
                    add_warning("enumerations", enumeration_samples, record_id, field, value)
                if value.strip() in _GENERIC_ENDPOINTS:
                    add_warning("generic_endpoints", generic_endpoint_samples, record_id, field, value)
            predicate = relation["predicate"]
            if _ENUMERATION_RE.search(predicate) or "하거나" in predicate or "거나" in predicate:
                add_warning("enumerations", enumeration_samples, record_id, "predicate", predicate)
            if unit.get("unit_kind") == "terminal_item" and not any(
                value and value in unit.get("unit_text", "") for value in triple
            ):
                add_warning(
                    "context_only_terminal_relations",
                    context_only_samples,
                    record_id,
                    "triple",
                    " | ".join(triple),
                )

    checks = {
        "semantic_unit_ids_unique": len(unit_ids) == len(set(unit_ids)),
        "raw_unit_ids_unique": len(raw_ids) == len(set(raw_ids)),
        "coverage_exact": not missing_ids and not extra_ids and len(unit_ids) == len(raw_ids),
        "source_order_matches": raw_ids == unit_ids,
        "source_fields_preserved": not source_mismatches,
        "relations_complete": not incomplete_relations,
        "relation_mention_ids_stable": not invalid_mentions,
        "triples_unique_within_record": not duplicate_triples,
    }
    return {
        "passed": all(checks.values()),
        "counts": {
            "semantic_units": len(units),
            "raw_svo_records": len(raw_records),
            "raw_relations": relation_count,
            "empty_relation_records": empty_record_count,
            "missing_semantic_units": len(missing_ids),
            "extra_raw_records": len(extra_ids),
        },
        "checks": checks,
        "quality_warning_counts": warning_counts,
        "quality_warnings": {
            "numeric_constraint_endpoint_samples": constraint_samples,
            "legal_qualifier_endpoint_samples": legal_qualifier_samples,
            "enumeration_samples": enumeration_samples,
            "generic_endpoint_samples": generic_endpoint_samples,
            "context_only_terminal_samples": context_only_samples,
        },
        "details": {
            "missing_semantic_unit_ids": missing_ids,
            "extra_semantic_unit_ids": extra_ids,
            "source_mismatch_ids": source_mismatches[:sample_limit],
            "invalid_relation_mention_ids": invalid_mentions[:sample_limit],
            "incomplete_relation_record_ids": incomplete_relations[:sample_limit],
            "duplicate_relation_mention_ids": duplicate_triples[:sample_limit],
        },
    }


def audit_artifacts(
    units: list[dict[str, Any]],
    raw_records: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    relation_types: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    unit_ids = [record["semantic_unit_id"] for record in units]
    raw_unit_ids = [record["semantic_unit_id"] for record in raw_records]
    relation_mentions = [
        relation["relation_mention_id"]
        for record in raw_records
        for relation in record.get("relations", [])
    ]
    entity_ids = {record["entity_id"] for record in entities}
    relation_type_ids = {record["relation_type_id"] for record in relation_types}
    relation_ids = [record["relation_id"] for record in edges]
    edge_evidence_mentions = [
        evidence["relation_mention_id"]
        for edge in edges
        for evidence in edge.get("evidence", [])
    ]
    missing_units = sorted(set(unit_ids) - set(raw_unit_ids))
    unknown_edge_entities = sorted(
        {
            entity_id
            for edge in edges
            for entity_id in (edge["source_entity_id"], edge["target_entity_id"])
            if entity_id not in entity_ids
        }
    )
    unknown_edge_types = sorted(
        {edge["relation_type_id"] for edge in edges if edge["relation_type_id"] not in relation_type_ids}
    )
    return {
        "counts": {
            "semantic_units": len(units),
            "raw_svo_records": len(raw_records),
            "raw_relations": len(relation_mentions),
            "empty_relation_records": sum(not record.get("relations") for record in raw_records),
            "entities": len(entities),
            "relation_types": len(relation_types),
            "normalized_edges": len(edges),
            "edge_evidence": sum(edge.get("evidence_count", len(edge.get("evidence", []))) for edge in edges),
        },
        "checks": {
            "semantic_unit_ids_unique": len(unit_ids) == len(set(unit_ids)),
            "raw_unit_ids_unique": len(raw_unit_ids) == len(set(raw_unit_ids)),
            "all_units_extracted": not missing_units,
            "relation_mention_ids_unique": len(relation_mentions) == len(set(relation_mentions)),
            "entity_ids_unique": len(entity_ids) == len(entities),
            "relation_type_ids_unique": len(relation_type_ids) == len(relation_types),
            "entity_ids_are_uuid_strings": all(_UUID_RE.fullmatch(value) for value in entity_ids),
            "relation_type_ids_are_uuid_strings": all(
                _UUID_RE.fullmatch(value) for value in relation_type_ids
            ),
            "relation_ids_are_uuid_strings": all(_UUID_RE.fullmatch(value) for value in relation_ids),
            "relation_ids_unique": len(relation_ids) == len(set(relation_ids)),
            "all_edge_entities_exist": not unknown_edge_entities,
            "all_edge_relation_types_exist": not unknown_edge_types,
            "edge_evidence_counts_match": all(
                edge.get("evidence_count") == len(edge.get("evidence", [])) for edge in edges
            ),
            "edge_evidence_mentions_unique": len(edge_evidence_mentions)
            == len(set(edge_evidence_mentions)),
            "all_relation_mentions_normalized": set(edge_evidence_mentions)
            == set(relation_mentions),
        },
        "missing_semantic_unit_ids": missing_units,
        "unknown_edge_entity_ids": unknown_edge_entities,
        "unknown_edge_relation_type_ids": unknown_edge_types,
    }


def audit_dictionary_release(
    raw_records: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    relation_types: list[dict[str, Any]],
    *,
    expected_relation_type_count: int = 98,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Audit a dictionary-build release without requiring graph load records."""

    entity_conflict = None
    relation_conflict = None
    try:
        entity_index = build_entity_alias_index(entities)
    except Exception as exc:
        entity_index = {}
        entity_conflict = str(exc)
    try:
        relation_index = build_relation_alias_index(relation_types)
    except Exception as exc:
        relation_index = {}
        relation_conflict = str(exc)

    missing_entity_names: set[str] = set()
    missing_predicates: set[str] = set()
    raw_entity_names: set[str] = set()
    raw_predicates: set[str] = set()
    for record in raw_records:
        for relation in record.get("relations", []):
            for raw_name in (relation["subject"], relation["object"]):
                raw_name = raw_name.strip()
                raw_entity_names.add(raw_name)
                resolved_name, _ = resolve_entity_name(
                    raw_name,
                    document_title=record["document_title"],
                )
                if resolved_name not in entity_index:
                    missing_entity_names.add(resolved_name)
            predicate = relation["predicate"].strip()
            raw_predicates.add(predicate)
            if predicate not in relation_index:
                missing_predicates.add(predicate)

    entity_ids = [str(record["entity_id"]) for record in entities]
    relation_type_ids = [
        str(record["relation_type_id"]) for record in relation_types
    ]
    entity_canonical_names = [record["canonical_name"] for record in entities]
    relation_canonical_names = [
        record["canonical_name"] for record in relation_types
    ]

    def canonical_alias_is_first(record: dict[str, Any]) -> bool:
        aliases = record.get("aliases", [])
        return bool(aliases) and aliases[0].get("name") == record["canonical_name"]

    def has_scope(record: dict[str, Any]) -> bool:
        return "scope" in record or any(
            isinstance(alias, dict) and "scope" in alias
            for alias in record.get("aliases", [])
        )

    self_reference_aliases = sorted(
        {
            alias.get("name")
            for record in entities
            for alias in record.get("aliases", [])
            if isinstance(alias, dict) and alias.get("name") in SELF_REFERENCES
        }
    )
    pure_numeric_entities = sorted(
        name
        for name in entity_canonical_names
        if _NUMERIC_CONSTRAINT_RE.fullmatch(name.strip())
    )
    checks = {
        "entity_aliases_have_no_conflicts": entity_conflict is None,
        "relation_aliases_have_no_conflicts": relation_conflict is None,
        "entity_ids_unique": len(entity_ids) == len(set(entity_ids)),
        "relation_type_ids_unique": len(relation_type_ids)
        == len(set(relation_type_ids)),
        "entity_canonical_names_unique": len(entity_canonical_names)
        == len(set(entity_canonical_names)),
        "relation_canonical_names_unique": len(relation_canonical_names)
        == len(set(relation_canonical_names)),
        "entity_ids_are_stable_uuids": all(
            _UUID_RE.fullmatch(identifier)
            and identifier == stable_id("entity", record["canonical_name"])
            for identifier, record in zip(entity_ids, entities)
        ),
        "all_entity_types_present": all(
            record.get("entity_type") for record in entities
        ),
        "all_entity_types_valid": all(
            record.get("entity_type") in ENTITY_TYPE_SET for record in entities
        ),
        "relation_type_ids_are_stable_uuids": all(
            _UUID_RE.fullmatch(identifier)
            and identifier == stable_id("relation_type", record["canonical_name"])
            for identifier, record in zip(relation_type_ids, relation_types)
        ),
        "canonical_entity_alias_is_first": all(
            canonical_alias_is_first(record) for record in entities
        ),
        "canonical_relation_alias_is_first": all(
            canonical_alias_is_first(record) for record in relation_types
        ),
        "no_scope_fields": not any(has_scope(record) for record in entities),
        "no_global_self_reference_aliases": not self_reference_aliases,
        "no_pure_numeric_entities": not pure_numeric_entities,
        "all_raw_entities_mapped": not missing_entity_names,
        "all_raw_predicates_mapped": not missing_predicates,
        "relation_type_count_is_expected": len(relation_types)
        == expected_relation_type_count,
        "relation_type_count_at_most_100": len(relation_types) <= 100,
    }
    return {
        "passed": all(checks.values()),
        "counts": {
            "raw_entity_surface_names": len(raw_entity_names),
            "raw_predicates": len(raw_predicates),
            "entities": len(entities),
            "entity_types": {
                entity_type: sum(
                    record.get("entity_type") == entity_type for record in entities
                )
                for entity_type in sorted(ENTITY_TYPE_SET)
            },
            "relation_types": len(relation_types),
            "entity_aliases": sum(len(record.get("aliases", [])) for record in entities),
            "relation_aliases": sum(
                len(record.get("aliases", [])) for record in relation_types
            ),
        },
        "checks": checks,
        "details": {
            "entity_alias_conflict": entity_conflict,
            "relation_alias_conflict": relation_conflict,
            "missing_entity_names": sorted(missing_entity_names)[:sample_limit],
            "missing_predicates": sorted(missing_predicates)[:sample_limit],
            "self_reference_aliases": self_reference_aliases,
            "pure_numeric_entities": pure_numeric_entities[:sample_limit],
        },
    }
