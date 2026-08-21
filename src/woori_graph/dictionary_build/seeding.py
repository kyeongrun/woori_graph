"""Reuse safe aliases from an earlier dictionary without carrying its scope."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from ..graph_mapping import build_relation_alias_index


_SUSPECT_MOJIBAKE_RE = re.compile(r"[\u00c0-\u024f\ufffd]")


def build_seeded_entity_mapping(
    source_entities: Sequence[dict[str, Any]],
    seed_dictionary: Sequence[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Map only unambiguous seed aliases; preserve every other source name.

    Scope fields in historical dictionaries are ignored. If an alias points
    to more than one canonical name (for example ``위원회``), it is not used.
    A name that is already a canonical name keeps itself even if it also
    appears as another entry's alias.
    """

    canonical_names = {
        str(record["canonical_name"]).strip() for record in seed_dictionary
    }
    alias_targets: dict[str, set[str]] = defaultdict(set)
    for record in seed_dictionary:
        canonical_name = str(record["canonical_name"]).strip()
        if not canonical_name or _SUSPECT_MOJIBAKE_RE.search(canonical_name):
            continue
        alias_targets[canonical_name].add(canonical_name)
        for alias in record.get("aliases", []):
            if not isinstance(alias, Mapping):
                continue
            name = str(alias.get("name", "")).strip()
            if name:
                alias_targets[name].add(canonical_name)

    mapping: dict[str, dict[str, str]] = {}
    for record in source_entities:
        source_name = str(record["canonical_name"]).strip()
        targets = alias_targets.get(source_name, set())
        if source_name in canonical_names:
            canonical_name = source_name
            status = "seed_exact_canonical"
        elif len(targets) == 1:
            canonical_name = next(iter(targets))
            status = "seed_unique_alias"
        else:
            canonical_name = source_name
            status = (
                "fallback_source_name_ambiguous_seed_alias"
                if len(targets) > 1
                else "fallback_source_name_no_seed_match"
            )
        mapping[source_name] = {
            "canonical_name": canonical_name,
            "normalization_status": status,
        }
    return mapping


def build_refreshed_relation_dictionary(
    raw_records: Sequence[dict[str, Any]],
    seed_dictionary: Sequence[dict[str, Any]],
    *,
    relation_overrides: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Keep the seed taxonomy and add aliases observed by a new extraction."""

    relation_index = build_relation_alias_index(seed_dictionary)
    relation_by_id = {
        record["relation_type_id"]: record for record in seed_dictionary
    }
    overrides = dict(relation_overrides or {})
    invalid_ids = sorted(set(overrides.values()) - set(relation_by_id))
    if invalid_ids:
        raise ValueError(f"unknown relation_type_ids in overrides: {invalid_ids}")

    buckets: dict[str, dict[str, Any]] = {}
    for seed in seed_dictionary:
        relation_type_id = seed["relation_type_id"]
        canonical_name = seed["canonical_name"]
        aliases: dict[str, dict[str, Any]] = {}
        for alias in seed.get("aliases", []):
            if not isinstance(alias, Mapping):
                continue
            name = str(alias.get("name", "")).strip()
            if name:
                aliases[name] = {
                    "name": name,
                    "mention_count": 0,
                    "sample_source_refs": [],
                }
        aliases.setdefault(
            canonical_name,
            {"name": canonical_name, "mention_count": 0, "sample_source_refs": []},
        )
        buckets[relation_type_id] = {
            "canonical_name": canonical_name,
            "relation_type_id": relation_type_id,
            "polarity": seed.get("polarity"),
            "mention_count": 0,
            "aliases": aliases,
        }

    missing: set[str] = set()
    for record in raw_records:
        sample = {
            "document_id": record["document_id"],
            "semantic_unit_id": record["semantic_unit_id"],
            "source_ref": record["source_ref"],
        }
        for relation in record.get("relations", []):
            predicate = relation["predicate"].strip()
            target = relation_index.get(predicate)
            if target is None and predicate in overrides:
                target = relation_by_id[overrides[predicate]]
            if target is None:
                missing.add(predicate)
                continue
            bucket = buckets[target["relation_type_id"]]
            bucket["mention_count"] += 1
            alias = bucket["aliases"].setdefault(
                predicate,
                {"name": predicate, "mention_count": 0, "sample_source_refs": []},
            )
            alias["mention_count"] += 1
            if sample not in alias["sample_source_refs"] and len(
                alias["sample_source_refs"]
            ) < 5:
                alias["sample_source_refs"].append(sample)
    if missing:
        from ..graph_mapping import UnmappedRelationError

        raise UnmappedRelationError(sorted(missing))

    output: list[dict[str, Any]] = []
    for bucket in buckets.values():
        aliases = list(bucket["aliases"].values())
        for alias in aliases:
            alias["is_canonical"] = alias["name"] == bucket["canonical_name"]
        aliases.sort(
            key=lambda item: (
                not item["is_canonical"],
                -item["mention_count"],
                item["name"],
            )
        )
        bucket["aliases"] = aliases
        output.append(bucket)
    output.sort(key=lambda item: (item["canonical_name"], item["relation_type_id"]))
    return output
