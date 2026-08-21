"""Map raw SVO records to load-ready graph records with released dictionaries."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .entity_resolution import resolve_entity_name
from .ids import stable_id


class DictionaryConflictError(ValueError):
    """Raised when one alias points to multiple dictionary entries."""


class UnmappedRelationError(ValueError):
    """Raised when graph construction would persist a free-form relation."""

    def __init__(self, predicates: Sequence[str]):
        self.predicates = tuple(sorted(set(predicates)))
        super().__init__(
            "Every relation must map to the released relation dictionary; "
            f"unmapped predicates={list(self.predicates)!r}"
        )


@dataclass(frozen=True)
class GraphLoadBundle:
    """Store-neutral records consumed by internal RDB/AGE/OpenSearch adapters."""

    documents: list[dict[str, Any]]
    entities: list[dict[str, Any]]
    relation_types: list[dict[str, Any]]
    relations: list[dict[str, Any]]
    entity_mapping_results: list[dict[str, Any]]
    relation_mapping_results: list[dict[str, Any]]
    unmapped_entities: list[dict[str, Any]]


def build_entity_alias_index(
    entity_dictionary: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return _build_alias_index(
        entity_dictionary,
        id_key="entity_id",
        dictionary_kind="entity",
    )


def build_relation_alias_index(
    relation_dictionary: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return _build_alias_index(
        relation_dictionary,
        id_key="relation_type_id",
        dictionary_kind="relation",
    )


def collect_unmapped_predicates(
    raw_records: Sequence[dict[str, Any]],
    relation_dictionary: Sequence[dict[str, Any]],
) -> list[str]:
    relation_index = build_relation_alias_index(relation_dictionary)
    return sorted(
        {
            relation["predicate"].strip()
            for record in raw_records
            for relation in record.get("relations", [])
            if relation["predicate"].strip() not in relation_index
        }
    )


def map_raw_svo_to_graph(
    raw_records: Sequence[dict[str, Any]],
    entity_dictionary: Sequence[dict[str, Any]],
    relation_dictionary: Sequence[dict[str, Any]],
    *,
    dictionary_version: str,
    relation_overrides: Mapping[str, str] | None = None,
) -> GraphLoadBundle:
    """Map raw triples and aggregate identical edges with evidence.

    Entity names absent from the dictionary are loaded under their unchanged
    surface name and a stable UUID. Relation names have no such fallback: an
    override or released-dictionary alias must resolve every predicate.
    """

    if not dictionary_version.strip():
        raise ValueError("dictionary_version must be non-empty")
    entity_index = build_entity_alias_index(entity_dictionary)
    relation_index = build_relation_alias_index(relation_dictionary)
    relation_by_id = {
        record["relation_type_id"]: record for record in relation_dictionary
    }
    overrides = dict(relation_overrides or {})
    invalid_override_ids = sorted(set(overrides.values()) - set(relation_by_id))
    if invalid_override_ids:
        raise ValueError(
            f"relation overrides reference unknown relation_type_ids: {invalid_override_ids}"
        )

    unmapped_predicates = {
        relation["predicate"].strip()
        for record in raw_records
        for relation in record.get("relations", [])
        if relation["predicate"].strip() not in relation_index
        and relation["predicate"].strip() not in overrides
    }
    if unmapped_predicates:
        raise UnmappedRelationError(sorted(unmapped_predicates))

    document_buckets: dict[str, dict[str, Any]] = {}
    entity_buckets: dict[str, dict[str, Any]] = {}
    edge_buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    entity_mapping_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    relation_mapping_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    unmapped_entity_buckets: dict[str, dict[str, Any]] = {}
    used_relation_type_ids: set[str] = set()

    for record in raw_records:
        document_id = record["document_id"]
        document_buckets.setdefault(
            document_id,
            {
                "document_id": document_id,
                "document_title": record["document_title"],
                "source_path": record.get("source_path", ""),
            },
        )
        for relation in record.get("relations", []):
            source = _resolve_entity_for_load(
                relation["subject"],
                entity_index,
                entity_buckets,
                entity_mapping_buckets,
                unmapped_entity_buckets,
                record,
                dictionary_version,
            )
            target = _resolve_entity_for_load(
                relation["object"],
                entity_index,
                entity_buckets,
                entity_mapping_buckets,
                unmapped_entity_buckets,
                record,
                dictionary_version,
            )
            raw_predicate = relation["predicate"].strip()
            relation_type = relation_index.get(raw_predicate)
            mapping_status = "dictionary_alias"
            if relation_type is None:
                relation_type = relation_by_id[overrides[raw_predicate]]
                mapping_status = "forced_dictionary_mapping"
            relation_type_id = relation_type["relation_type_id"]
            used_relation_type_ids.add(relation_type_id)
            relation_mapping_key = (raw_predicate, relation_type_id)
            relation_mapping_bucket = relation_mapping_buckets.setdefault(
                relation_mapping_key,
                {
                    "raw_predicate": raw_predicate,
                    "relation_type_id": relation_type_id,
                    "canonical_name": relation_type["canonical_name"],
                    "polarity": relation_type.get("polarity"),
                    "mapping_status": mapping_status,
                    "dictionary_version": dictionary_version,
                    "mention_count": 0,
                },
            )
            relation_mapping_bucket["mention_count"] += 1

            edge_key = (source["entity_id"], relation_type_id, target["entity_id"])
            edge = edge_buckets.setdefault(
                edge_key,
                {
                    "relation_id": stable_id("relation", *edge_key),
                    "source_entity_id": source["entity_id"],
                    "relation_type_id": relation_type_id,
                    "target_entity_id": target["entity_id"],
                    "source_name": source["canonical_name"],
                    "target_name": target["canonical_name"],
                    "dictionary_version": dictionary_version,
                    "evidence": [],
                },
            )
            edge["evidence"].append(
                {
                    "relation_mention_id": relation["relation_mention_id"],
                    "semantic_unit_id": record["semantic_unit_id"],
                    "document_id": document_id,
                    "source_ref": record["source_ref"],
                    "raw_subject": relation["subject"],
                    "raw_predicate": raw_predicate,
                    "raw_object": relation["object"],
                }
            )

    entities = []
    for bucket in entity_buckets.values():
        aliases = list(bucket.pop("_aliases").values())
        aliases.sort(
            key=lambda item: (
                not item["is_canonical"],
                -item["mention_count"],
                item["name"],
            )
        )
        bucket["aliases"] = aliases
        entities.append(bucket)
    entities.sort(key=lambda item: (item["canonical_name"], item["entity_id"]))

    relations = list(edge_buckets.values())
    for relation in relations:
        relation["evidence_count"] = len(relation["evidence"])
    relations.sort(key=lambda item: item["relation_id"])

    relation_types = [
        _load_relation_type_record(relation_by_id[relation_type_id], dictionary_version)
        for relation_type_id in sorted(used_relation_type_ids)
    ]
    unmapped_entities = sorted(
        unmapped_entity_buckets.values(), key=lambda item: item["canonical_name"]
    )
    return GraphLoadBundle(
        documents=sorted(document_buckets.values(), key=lambda item: item["document_id"]),
        entities=entities,
        relation_types=relation_types,
        relations=relations,
        entity_mapping_results=sorted(
            entity_mapping_buckets.values(),
            key=lambda item: (item["raw_name"], item["entity_id"]),
        ),
        relation_mapping_results=sorted(
            relation_mapping_buckets.values(),
            key=lambda item: (item["raw_predicate"], item["relation_type_id"]),
        ),
        unmapped_entities=unmapped_entities,
    )


def _build_alias_index(
    dictionary: Sequence[dict[str, Any]],
    *,
    id_key: str,
    dictionary_kind: str,
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in dictionary:
        canonical_name = str(record["canonical_name"]).strip()
        identifier = str(record[id_key]).strip()
        if not canonical_name or not identifier:
            raise ValueError(f"{dictionary_kind} dictionary contains an empty name or ID")
        names = [canonical_name]
        names.extend(
            str(alias.get("name", "")).strip()
            for alias in record.get("aliases", [])
            if isinstance(alias, Mapping)
        )
        for name in names:
            if not name:
                continue
            existing = index.get(name)
            if existing is not None and existing[id_key] != identifier:
                raise DictionaryConflictError(
                    f"{dictionary_kind} alias {name!r} maps to both "
                    f"{existing[id_key]!r} and {identifier!r}"
                )
            index[name] = record
    return index


def _resolve_entity_for_load(
    raw_name: str,
    entity_index: Mapping[str, dict[str, Any]],
    entity_buckets: dict[str, dict[str, Any]],
    mapping_buckets: dict[tuple[str, str], dict[str, Any]],
    unmapped_buckets: dict[str, dict[str, Any]],
    source_record: dict[str, Any],
    dictionary_version: str,
) -> dict[str, Any]:
    raw_name = raw_name.strip()
    resolved_name, resolution_method = resolve_entity_name(
        raw_name,
        document_title=source_record["document_title"],
    )
    dictionary_entity = entity_index.get(resolved_name)
    if dictionary_entity is None:
        entity_id = stable_id("entity", resolved_name)
        canonical_name = resolved_name
        entity_type = "OTHER"
        mapping_status = (
            resolution_method
            if resolution_method == "current_document_self_reference"
            else "new_raw_entity"
        )
        dictionary_match = False
    else:
        entity_id = dictionary_entity["entity_id"]
        canonical_name = dictionary_entity["canonical_name"]
        entity_type = dictionary_entity.get("entity_type", "OTHER")
        if resolution_method == "current_document_self_reference":
            mapping_status = resolution_method
        else:
            mapping_status = (
                "dictionary_canonical"
                if raw_name == canonical_name
                else "dictionary_alias"
            )
        dictionary_match = True

    bucket = entity_buckets.setdefault(
        entity_id,
        {
            "entity_id": entity_id,
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "dictionary_match": dictionary_match,
            "dictionary_version": dictionary_version,
            "mention_count": 0,
            "_aliases": {},
        },
    )
    bucket["mention_count"] += 1
    alias_names = [canonical_name]
    if resolution_method != "current_document_self_reference":
        alias_names.append(raw_name)
    for alias_name in dict.fromkeys(alias_names):
        alias = bucket["_aliases"].setdefault(
            alias_name,
            {
                "name": alias_name,
                "is_canonical": alias_name == canonical_name,
                "mention_count": 0,
            },
        )
        if alias_name == raw_name or (
            resolution_method == "current_document_self_reference"
            and alias_name == canonical_name
        ):
            alias["mention_count"] += 1

    mapping_key = (raw_name, entity_id)
    mapping = mapping_buckets.setdefault(
        mapping_key,
        {
            "raw_name": raw_name,
            "resolved_name": resolved_name,
            "entity_id": entity_id,
            "canonical_name": canonical_name,
            "mapping_status": mapping_status,
            "dictionary_version": dictionary_version,
            "mention_count": 0,
        },
    )
    mapping["mention_count"] += 1

    if not dictionary_match:
        unmapped = unmapped_buckets.setdefault(
            resolved_name,
            {
                "canonical_name": resolved_name,
                "entity_id": entity_id,
                "mention_count": 0,
                "sample_source_refs": [],
            },
        )
        unmapped["mention_count"] += 1
        sample = {
            "document_id": source_record["document_id"],
            "semantic_unit_id": source_record["semantic_unit_id"],
            "source_ref": source_record["source_ref"],
        }
        if sample not in unmapped["sample_source_refs"] and len(
            unmapped["sample_source_refs"]
        ) < 5:
            unmapped["sample_source_refs"].append(sample)
    return bucket


def _load_relation_type_record(
    dictionary_record: dict[str, Any], dictionary_version: str
) -> dict[str, Any]:
    return {
        "relation_type_id": dictionary_record["relation_type_id"],
        "canonical_name": dictionary_record["canonical_name"],
        "polarity": dictionary_record.get("polarity"),
        "dictionary_version": dictionary_version,
    }
