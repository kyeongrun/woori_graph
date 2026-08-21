"""Build deterministic RDB, Apache AGE, and OpenSearch load files."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..embeddings import (
    EmbeddingClient,
    entity_embedding_text,
    normalize_embedding,
    relation_embedding_text,
)
from ..graph_mapping import GraphLoadBundle, map_raw_svo_to_graph
from ..jsonl import write_jsonl


_AGE_GRAPH_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_INDEX_PART_RE = re.compile(r"[^a-z0-9]+")
RDB_SCHEMA = "graph"
POSTGRES_DATABASE = "graphdb"
DEFAULT_AGE_GRAPH_NAME = "svo"
ENTITY_INDEX_ALIAS = "entities"
RELATION_INDEX_ALIAS = "relations"


def relation_overrides_from_records(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Read a forced relation map while rejecting conflicting duplicate rows."""

    overrides: dict[str, str] = {}
    for record in records:
        predicate = str(record.get("raw_predicate", "")).strip()
        relation_type_id = str(record.get("relation_type_id", "")).strip()
        if not predicate or not relation_type_id:
            raise ValueError("relation override requires raw_predicate and relation_type_id")
        existing = overrides.get(predicate)
        if existing is not None and existing != relation_type_id:
            raise ValueError(
                f"conflicting relation override for {predicate!r}: "
                f"{existing!r} and {relation_type_id!r}"
            )
        overrides[predicate] = relation_type_id
    return overrides


def build_storage_load_files(
    raw_records: Sequence[dict[str, Any]],
    entity_dictionary: Sequence[dict[str, Any]],
    relation_dictionary: Sequence[dict[str, Any]],
    *,
    dictionary_version: str,
    output_dir: Path,
    age_graph_name: str = DEFAULT_AGE_GRAPH_NAME,
    relation_overrides: Mapping[str, str] | None = None,
    embedding_client: EmbeddingClient | None = None,
    embedding_batch_size: int = 64,
    overwrite: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create one self-contained, auditable load directory for all stores.

    AGE owns numeric internal graph IDs, so CSV import uses temporary positive
    integers for topology. Immediately after import, the public ``id`` property
    is set to the exact UUID string also used by PostgreSQL and OpenSearch.
    """

    dictionary_version = dictionary_version.strip()
    if not dictionary_version:
        raise ValueError("dictionary_version must be non-empty")
    if not _AGE_GRAPH_RE.fullmatch(age_graph_name):
        raise ValueError(
            "age_graph_name must start with a lowercase letter and contain only "
            "lowercase letters, digits, or underscores (maximum 63 characters)"
        )
    if embedding_batch_size < 1:
        raise ValueError("embedding_batch_size must be at least 1")
    if embedding_client is not None and not 1 <= embedding_client.dimension <= 16000:
        raise ValueError("embedding dimension must be between 1 and 16000")
    manifest_path = output_dir / "load_manifest.json"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing load release: {output_dir}")

    timestamp = created_at or datetime.now().astimezone().isoformat(timespec="seconds")
    bundle = map_raw_svo_to_graph(
        raw_records,
        entity_dictionary,
        relation_dictionary,
        dictionary_version=dictionary_version,
        relation_overrides=relation_overrides,
    )
    records = _materialize_records(
        bundle,
        entity_dictionary,
        relation_dictionary,
    )

    records_dir = output_dir / "records"
    rdb_dir = output_dir / "rdb"
    age_dir = output_dir / "age"
    opensearch_dir = output_dir / "opensearch"
    for directory in (records_dir, rdb_dir, age_dir, opensearch_dir):
        directory.mkdir(parents=True, exist_ok=True)

    _write_record_jsonl(records_dir, records, overwrite=overwrite)
    _write_rdb_files(
        rdb_dir,
        records,
        overwrite=overwrite,
    )
    age_metadata = _write_age_files(
        age_dir,
        records,
        age_graph_name=age_graph_name,
        container_release_dir=f"/runtime/load/{output_dir.name}",
        overwrite=overwrite,
    )
    opensearch_metadata = _write_opensearch_files(
        opensearch_dir,
        records,
        dictionary_version=dictionary_version,
        embedding_client=embedding_client,
        embedding_batch_size=embedding_batch_size,
        overwrite=overwrite,
    )

    generated_files = sorted(
        path
        for path in output_dir.rglob("*")
        if path.is_file()
        and path != manifest_path
        and path.name != "load_reconcile.json"
    )
    counts = {
        "documents": len(records["documents"]),
        "entities": len(records["entities"]),
        "relation_types": len(records["relation_types"]),
        "relations": len(records["relations"]),
        "evidence": len(records["evidence"]),
        "entity_mapping_results": len(bundle.entity_mapping_results),
        "relation_mapping_results": len(bundle.relation_mapping_results),
        "unmapped_entities": len(bundle.unmapped_entities),
    }
    checks = _audit_cross_store_files(output_dir, records, opensearch_metadata)
    manifest = {
        "dictionary_version": dictionary_version,
        "created_at": timestamp,
        "age_graph_name": age_graph_name,
        "rdb": {
            "database": POSTGRES_DATABASE,
            "schema": RDB_SCHEMA,
            "tables": [
                "document",
                "entity",
                "relation_type",
                "relation",
                "relation_evidence",
            ],
        },
        "opensearch": opensearch_metadata,
        "age": age_metadata,
        "counts": counts,
        "checks": checks,
        "passed": all(checks.values()),
        "files": [
            {
                "path": path.relative_to(output_dir).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in generated_files
        ],
    }
    _write_json(manifest_path, manifest, overwrite=overwrite)
    return manifest


def _materialize_records(
    bundle: GraphLoadBundle,
    entity_dictionary: Sequence[dict[str, Any]],
    relation_dictionary: Sequence[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    entity_dictionary_by_id = {
        str(record["entity_id"]): record for record in entity_dictionary
    }
    relation_dictionary_by_id = {
        str(record["relation_type_id"]): record for record in relation_dictionary
    }

    entities: list[dict[str, Any]] = []
    for record in bundle.entities:
        dictionary_record = entity_dictionary_by_id.get(record["entity_id"])
        aliases = (
            dictionary_record.get("aliases", [])
            if dictionary_record is not None
            else record.get("aliases", [])
        )
        entities.append(
            {
                "entity_id": record["entity_id"],
                "canonical_name": record["canonical_name"],
                "entity_type": record.get("entity_type") or "OTHER",
                "aliases": [
                    str(alias.get("name", "")).strip()
                    for alias in aliases
                    if str(alias.get("name", "")).strip()
                ],
            }
        )
    entities.sort(key=lambda item: (item["canonical_name"], item["entity_id"]))

    relation_types: list[dict[str, Any]] = []
    for record in bundle.relation_types:
        dictionary_record = relation_dictionary_by_id[record["relation_type_id"]]
        relation_types.append(
            {
                "relation_type_id": record["relation_type_id"],
                "canonical_name": record["canonical_name"],
                "polarity": record.get("polarity"),
                "aliases": [
                    str(alias.get("name", "")).strip()
                    for alias in dictionary_record.get("aliases", [])
                    if str(alias.get("name", "")).strip()
                ],
            }
        )
    relation_types.sort(key=lambda item: item["relation_type_id"])

    evidence: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for record in bundle.relations:
        relation = {
            key: value
            for key, value in record.items()
            if key not in {"evidence", "dictionary_version"}
        }
        relations.append(relation)
        for item in record.get("evidence", []):
            evidence.append({"relation_id": record["relation_id"], **item})
    evidence.sort(key=lambda item: item["relation_mention_id"])

    return {
        "documents": list(bundle.documents),
        "entities": entities,
        "relation_types": relation_types,
        "relations": relations,
        "evidence": evidence,
    }


def _write_record_jsonl(
    records_dir: Path,
    records: Mapping[str, Sequence[dict[str, Any]]],
    *,
    overwrite: bool,
) -> None:
    for name in ("documents", "entities", "relation_types", "relations", "evidence"):
        write_jsonl(
            records_dir / f"{name}.jsonl",
            records[name],
            overwrite=overwrite,
        )


def _write_rdb_files(
    rdb_dir: Path,
    records: Mapping[str, Sequence[dict[str, Any]]],
    *,
    overwrite: bool,
) -> None:
    _write_csv(
        rdb_dir / "documents.csv",
        ["document_id", "document_title", "source_path"],
        (
            [
                item["document_id"],
                item["document_title"],
                item.get("source_path", ""),
            ]
            for item in records["documents"]
        ),
        overwrite=overwrite,
    )
    _write_csv(
        rdb_dir / "entities.csv",
        [
            "entity_id",
            "canonical_name",
            "entity_type",
            "aliases_json",
        ],
        (
            [
                item["entity_id"],
                item["canonical_name"],
                item["entity_type"],
                _compact_json(item["aliases"]),
            ]
            for item in records["entities"]
        ),
        overwrite=overwrite,
    )
    _write_csv(
        rdb_dir / "relation_types.csv",
        [
            "relation_type_id",
            "canonical_name",
            "polarity",
            "aliases_json",
        ],
        (
            [
                item["relation_type_id"],
                item["canonical_name"],
                item.get("polarity") or "",
                _compact_json(item["aliases"]),
            ]
            for item in records["relation_types"]
        ),
        overwrite=overwrite,
    )
    _write_csv(
        rdb_dir / "relations.csv",
        [
            "relation_id",
            "source_entity_id",
            "relation_type_id",
            "target_entity_id",
            "source_name",
            "target_name",
            "evidence_count",
        ],
        (
            [
                item["relation_id"],
                item["source_entity_id"],
                item["relation_type_id"],
                item["target_entity_id"],
                item["source_name"],
                item["target_name"],
                item["evidence_count"],
            ]
            for item in records["relations"]
        ),
        overwrite=overwrite,
    )
    _write_csv(
        rdb_dir / "relation_evidence.csv",
        [
            "relation_mention_id",
            "relation_id",
            "semantic_unit_id",
            "document_id",
            "source_ref_json",
            "raw_subject",
            "raw_predicate",
            "raw_object",
        ],
        (
            [
                item["relation_mention_id"],
                item["relation_id"],
                item["semantic_unit_id"],
                item["document_id"],
                _compact_json(item["source_ref"]),
                item["raw_subject"],
                item["raw_predicate"],
                item["raw_object"],
            ]
            for item in records["evidence"]
        ),
        overwrite=overwrite,
    )
    _write_text(rdb_dir / "schema.sql", _rdb_schema_sql(), overwrite=overwrite)
    _write_text(
        rdb_dir / "load.sql",
        _rdb_load_sql(rdb_dir.parent.name),
        overwrite=overwrite,
    )


def _rdb_schema_sql() -> str:
    return """\
CREATE SCHEMA IF NOT EXISTS graph;

CREATE TABLE IF NOT EXISTS graph.document (
    document_id uuid PRIMARY KEY,
    document_title text NOT NULL,
    source_path text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS graph.entity (
    entity_id uuid PRIMARY KEY,
    canonical_name text NOT NULL,
    entity_type text NOT NULL CHECK (
        entity_type IN ('ORGANIZATION', 'PERSON', 'LEGAL_INSTRUMENT', 'CONCEPT', 'OTHER')
    ),
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS graph.relation_type (
    relation_type_id uuid PRIMARY KEY,
    canonical_name text NOT NULL,
    polarity text,
    aliases jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS graph.relation (
    relation_id uuid PRIMARY KEY,
    source_entity_id uuid NOT NULL REFERENCES graph.entity(entity_id),
    relation_type_id uuid NOT NULL REFERENCES graph.relation_type(relation_type_id),
    target_entity_id uuid NOT NULL REFERENCES graph.entity(entity_id),
    source_name text NOT NULL,
    target_name text NOT NULL,
    evidence_count bigint NOT NULL CHECK (evidence_count > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_entity_id, relation_type_id, target_entity_id)
);

CREATE TABLE IF NOT EXISTS graph.relation_evidence (
    relation_mention_id uuid PRIMARY KEY,
    relation_id uuid NOT NULL REFERENCES graph.relation(relation_id),
    semantic_unit_id uuid NOT NULL,
    document_id uuid NOT NULL REFERENCES graph.document(document_id),
    source_ref jsonb NOT NULL,
    raw_subject text NOT NULL,
    raw_predicate text NOT NULL,
    raw_object text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS entity_canonical_name_idx
    ON graph.entity (canonical_name);
CREATE INDEX IF NOT EXISTS entity_aliases_gin_idx
    ON graph.entity USING gin (aliases);
CREATE INDEX IF NOT EXISTS relation_source_idx
    ON graph.relation (source_entity_id);
CREATE INDEX IF NOT EXISTS relation_target_idx
    ON graph.relation (target_entity_id);
CREATE INDEX IF NOT EXISTS relation_type_idx
    ON graph.relation (relation_type_id);
CREATE INDEX IF NOT EXISTS relation_evidence_relation_idx
    ON graph.relation_evidence (relation_id);
CREATE INDEX IF NOT EXISTS relation_evidence_document_idx
    ON graph.relation_evidence (document_id);
"""


def _rdb_load_sql(release_name: str) -> str:
    release = _sql_literal(release_name)
    base = "/runtime/load"
    return rf"""
\set ON_ERROR_STOP on
SET client_encoding = 'UTF8';
BEGIN;

CREATE TEMP TABLE stage_document (
    document_id text, document_title text, source_path text
);
CREATE TEMP TABLE stage_entity (
    entity_id text, canonical_name text, entity_type text, aliases_json text
);
CREATE TEMP TABLE stage_relation_type (
    relation_type_id text, canonical_name text, polarity text, aliases_json text
);
CREATE TEMP TABLE stage_relation (
    relation_id text, source_entity_id text, relation_type_id text,
    target_entity_id text, source_name text, target_name text,
    evidence_count text
);
CREATE TEMP TABLE stage_relation_evidence (
    relation_mention_id text, relation_id text, semantic_unit_id text,
    document_id text, source_ref_json text, raw_subject text,
    raw_predicate text, raw_object text
);

\copy stage_document FROM '{base}/{release}/rdb/documents.csv' WITH (FORMAT csv, HEADER true)
\copy stage_entity FROM '{base}/{release}/rdb/entities.csv' WITH (FORMAT csv, HEADER true)
\copy stage_relation_type FROM '{base}/{release}/rdb/relation_types.csv' WITH (FORMAT csv, HEADER true)
\copy stage_relation FROM '{base}/{release}/rdb/relations.csv' WITH (FORMAT csv, HEADER true)
\copy stage_relation_evidence FROM '{base}/{release}/rdb/relation_evidence.csv' WITH (FORMAT csv, HEADER true)

INSERT INTO graph.document (
    document_id, document_title, source_path
)
SELECT document_id::uuid, document_title, source_path
FROM stage_document
ON CONFLICT (document_id) DO UPDATE SET
    document_title = EXCLUDED.document_title,
    source_path = EXCLUDED.source_path,
    updated_at = now();

INSERT INTO graph.entity (
    entity_id, canonical_name, entity_type, aliases
)
SELECT entity_id::uuid, canonical_name, entity_type, aliases_json::jsonb
FROM stage_entity
ON CONFLICT (entity_id) DO UPDATE SET
    canonical_name = EXCLUDED.canonical_name,
    entity_type = EXCLUDED.entity_type,
    aliases = EXCLUDED.aliases,
    updated_at = now();

INSERT INTO graph.relation_type (
    relation_type_id, canonical_name, polarity, aliases
)
SELECT relation_type_id::uuid, canonical_name, NULLIF(polarity, ''),
       aliases_json::jsonb
FROM stage_relation_type
ON CONFLICT (relation_type_id) DO UPDATE SET
    canonical_name = EXCLUDED.canonical_name,
    polarity = EXCLUDED.polarity,
    aliases = EXCLUDED.aliases,
    updated_at = now();

INSERT INTO graph.relation (
    relation_id, source_entity_id, relation_type_id, target_entity_id,
    source_name, target_name, evidence_count
)
SELECT relation_id::uuid, source_entity_id::uuid, relation_type_id::uuid,
       target_entity_id::uuid, source_name, target_name, evidence_count::bigint
FROM stage_relation
ON CONFLICT (relation_id) DO UPDATE SET
    source_entity_id = EXCLUDED.source_entity_id,
    relation_type_id = EXCLUDED.relation_type_id,
    target_entity_id = EXCLUDED.target_entity_id,
    source_name = EXCLUDED.source_name,
    target_name = EXCLUDED.target_name,
    evidence_count = EXCLUDED.evidence_count,
    updated_at = now();

INSERT INTO graph.relation_evidence (
    relation_mention_id, relation_id, semantic_unit_id, document_id,
    source_ref, raw_subject, raw_predicate, raw_object
)
SELECT relation_mention_id::uuid, relation_id::uuid, semantic_unit_id::uuid,
       document_id::uuid, source_ref_json::jsonb, raw_subject,
       raw_predicate, raw_object
FROM stage_relation_evidence
ON CONFLICT (relation_mention_id) DO UPDATE SET
    relation_id = EXCLUDED.relation_id,
    semantic_unit_id = EXCLUDED.semantic_unit_id,
    document_id = EXCLUDED.document_id,
    source_ref = EXCLUDED.source_ref,
    raw_subject = EXCLUDED.raw_subject,
    raw_predicate = EXCLUDED.raw_predicate,
    raw_object = EXCLUDED.raw_object,
    updated_at = now();

COMMIT;
""".lstrip()


def _write_age_files(
    age_dir: Path,
    records: Mapping[str, Sequence[dict[str, Any]]],
    *,
    age_graph_name: str,
    container_release_dir: str,
    overwrite: bool,
) -> dict[str, Any]:
    entity_numeric_ids = {
        item["entity_id"]: index
        for index, item in enumerate(records["entities"], start=1)
    }
    _write_csv(
        age_dir / "entities.csv",
        ["id", "uuid", "name"],
        (
            [entity_numeric_ids[item["entity_id"]], item["entity_id"], item["canonical_name"]]
            for item in records["entities"]
        ),
        overwrite=overwrite,
    )

    relation_type_names = {
        item["relation_type_id"]: item["canonical_name"]
        for item in records["relation_types"]
    }
    relations_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for relation in records["relations"]:
        relations_by_type[relation["relation_type_id"]].append(relation)

    edge_files: list[dict[str, Any]] = []
    for relation_type_id in sorted(relations_by_type):
        label = relation_type_names[relation_type_id]
        filename = f"edges_{relation_type_id}.csv"
        _write_csv(
            age_dir / filename,
            [
                "start_id",
                "start_vertex_type",
                "end_id",
                "end_vertex_type",
                "uuid",
                "source_name",
                "target_name",
            ],
            (
                [
                    entity_numeric_ids[item["source_entity_id"]],
                    "Entity",
                    entity_numeric_ids[item["target_entity_id"]],
                    "Entity",
                    item["relation_id"],
                    item["source_name"],
                    item["target_name"],
                ]
                for item in relations_by_type[relation_type_id]
            ),
            overwrite=overwrite,
        )
        edge_files.append(
            {
                "relation_type_id": relation_type_id,
                "label": label,
                "file": filename,
                "count": len(relations_by_type[relation_type_id]),
            }
        )

    sql_lines = [
        "\\set ON_ERROR_STOP on",
        "CREATE EXTENSION IF NOT EXISTS age;",
        "LOAD 'age';",
        'SET search_path = ag_catalog, "$user", public;',
        f"SELECT create_graph('{_sql_literal(age_graph_name)}');",
        f"SELECT create_vlabel('{_sql_literal(age_graph_name)}', 'Entity');",
        "SELECT load_labels_from_file(",
        f"    '{_sql_literal(age_graph_name)}', 'Entity',",
        f"    '{_sql_literal(container_release_dir)}/age/entities.csv'",
        ");",
        "SELECT * FROM cypher(",
        f"    '{_sql_literal(age_graph_name)}',",
        "    $$ MATCH (n:Entity) SET n.id = n.uuid REMOVE n.uuid $$",
        ") AS (result agtype);",
    ]
    for item in edge_files:
        label = _sql_literal(item["label"])
        cypher_label = item["label"].replace("`", "``")
        sql_lines.extend(
            [
                f"SELECT create_elabel('{_sql_literal(age_graph_name)}', '{label}');",
                "SELECT load_edges_from_file(",
                f"    '{_sql_literal(age_graph_name)}', '{label}',",
                f"    '{_sql_literal(container_release_dir)}/age/{item['file']}'",
                ");",
                "SELECT * FROM cypher(",
                f"    '{_sql_literal(age_graph_name)}',",
                f"    $$ MATCH ()-[r:`{cypher_label}`]->() "
                "SET r.id = r.uuid REMOVE r.uuid $$",
                ") AS (result agtype);",
            ]
        )
    graph_schema = age_graph_name.replace('"', '""')
    sql_lines.extend(
        [
            "SELECT * FROM cypher(",
            f"    '{_sql_literal(age_graph_name)}',",
            "    $$ MATCH (n:Entity) REMOVE n.__id__ $$",
            ") AS (result agtype);",
            f'CREATE INDEX "{age_graph_name}_entity_properties_gin"',
            f'    ON "{graph_schema}"."Entity" USING gin (properties);',
            "",
        ]
    )
    _write_text(age_dir / "load.sql", "\n".join(sql_lines), overwrite=overwrite)
    return {
        "vertex_label": "Entity",
        "edge_labels": edge_files,
        "internal_id_note": (
            "AGE graph IDs are numeric internals; public vertex/edge property id "
            "is the shared UUID string. Temporary CSV uuid properties are renamed to id."
        ),
    }


def _write_opensearch_files(
    opensearch_dir: Path,
    records: Mapping[str, Sequence[dict[str, Any]]],
    *,
    dictionary_version: str,
    embedding_client: EmbeddingClient | None,
    embedding_batch_size: int,
    overwrite: bool,
) -> dict[str, Any]:
    version_part = _index_name_part(dictionary_version)
    entity_index = f"entities-{version_part}"
    relation_index = f"relations-{version_part}"
    index_settings: dict[str, Any] = {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    }
    if embedding_client is not None:
        index_settings["knn"] = True
    entity_properties: dict[str, Any] = {
        "entity_id": {"type": "keyword"},
        "canonical_name": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}},
        },
        "entity_type": {"type": "keyword"},
        "aliases": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}},
        },
    }
    relation_properties: dict[str, Any] = {
        "relation_id": {"type": "keyword"},
        "source_entity_id": {"type": "keyword"},
        "relation_type_id": {"type": "keyword"},
        "relation_type_name": {"type": "keyword"},
        "target_entity_id": {"type": "keyword"},
        "source_name": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}},
        },
        "target_name": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 1024}},
        },
        "evidence_count": {"type": "long"},
    }
    if embedding_client is not None:
        vector_mapping = {
            "type": "knn_vector",
            "dimension": embedding_client.dimension,
            "method": {
                "name": "hnsw",
                "engine": "lucene",
                "space_type": "cosinesimil",
                "parameters": {"ef_construction": 100, "m": 16},
            },
        }
        entity_properties["embedding"] = vector_mapping
        relation_properties["embedding"] = vector_mapping
    entity_mapping = {
        "settings": {"index": index_settings},
        "mappings": {
            "dynamic": "strict",
            "properties": entity_properties,
        },
    }
    relation_mapping = {
        "settings": {"index": index_settings},
        "mappings": {
            "dynamic": "strict",
            "properties": relation_properties,
        },
    }
    _write_json(opensearch_dir / "entities.mapping.json", entity_mapping, overwrite=overwrite)
    _write_json(opensearch_dir / "relations.mapping.json", relation_mapping, overwrite=overwrite)

    relation_type_names = {
        item["relation_type_id"]: item["canonical_name"]
        for item in records["relation_types"]
    }
    entity_bulk = opensearch_dir / "entities.bulk.ndjson"
    relation_bulk = opensearch_dir / "relations.bulk.ndjson"
    entity_items = [
        (item["entity_id"], item, entity_embedding_text(item))
        for item in records["entities"]
    ]
    relation_items = []
    for item in records["relations"]:
        relation_type_name = relation_type_names[item["relation_type_id"]]
        document = {**item, "relation_type_name": relation_type_name}
        relation_items.append(
            (
                item["relation_id"],
                document,
                relation_embedding_text(
                    item,
                    relation_type_name=relation_type_name,
                ),
            )
        )
    if embedding_client is None:
        _write_bulk(
            entity_bulk,
            entity_index,
            ((identifier, document) for identifier, document, _ in entity_items),
            overwrite=overwrite,
        )
        _write_bulk(
            relation_bulk,
            relation_index,
            ((identifier, document) for identifier, document, _ in relation_items),
            overwrite=overwrite,
        )
    else:
        _write_embedded_bulk(
            entity_bulk,
            entity_index,
            entity_items,
            embedding_client,
            batch_size=embedding_batch_size,
            overwrite=overwrite,
        )
        _write_embedded_bulk(
            relation_bulk,
            relation_index,
            relation_items,
            embedding_client,
            batch_size=embedding_batch_size,
            overwrite=overwrite,
        )
    return {
        "entity_index": entity_index,
        "entity_alias": ENTITY_INDEX_ALIAS,
        "relation_index": relation_index,
        "relation_alias": RELATION_INDEX_ALIAS,
        "embedding": {
            "enabled": embedding_client is not None,
            "field": "embedding" if embedding_client is not None else None,
            "model": embedding_client.model if embedding_client is not None else None,
            "dimension": embedding_client.dimension if embedding_client is not None else None,
            "space_type": "cosinesimil" if embedding_client is not None else None,
            "engine": "lucene" if embedding_client is not None else None,
        },
    }


def _audit_cross_store_files(
    output_dir: Path,
    records: Mapping[str, Sequence[dict[str, Any]]],
    opensearch_metadata: Mapping[str, str],
) -> dict[str, bool]:
    entity_ids = {item["entity_id"] for item in records["entities"]}
    relation_ids = {item["relation_id"] for item in records["relations"]}
    relation_type_ids = {item["relation_type_id"] for item in records["relation_types"]}
    age_vertex_uuids = _csv_values(output_dir / "age" / "entities.csv", "uuid")
    age_relation_uuids: set[str] = set()
    for path in (output_dir / "age").glob("edges_*.csv"):
        age_relation_uuids.update(_csv_values(path, "uuid"))
    os_entity_ids = _bulk_ids(output_dir / "opensearch" / "entities.bulk.ndjson")
    os_relation_ids = _bulk_ids(output_dir / "opensearch" / "relations.bulk.ndjson")
    return {
        "all_entity_types_present": all(item.get("entity_type") for item in records["entities"]),
        "all_relation_endpoints_exist": all(
            item["source_entity_id"] in entity_ids and item["target_entity_id"] in entity_ids
            for item in records["relations"]
        ),
        "all_relation_types_exist": all(
            item["relation_type_id"] in relation_type_ids for item in records["relations"]
        ),
        "rdb_age_entity_ids_equal": entity_ids == age_vertex_uuids,
        "rdb_age_relation_ids_equal": relation_ids == age_relation_uuids,
        "rdb_opensearch_entity_ids_equal": entity_ids == os_entity_ids,
        "rdb_opensearch_relation_ids_equal": relation_ids == os_relation_ids,
        "opensearch_indices_are_versioned": (
            opensearch_metadata["entity_index"] != opensearch_metadata["entity_alias"]
            and opensearch_metadata["relation_index"] != opensearch_metadata["relation_alias"]
        ),
    }


def _write_csv(
    path: Path,
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]] | Any,
    *,
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w"
    with path.open(mode, encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def _write_bulk(
    path: Path,
    index_name: str,
    items: Any,
    *,
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for identifier, document in items:
            handle.write(_compact_json({"index": {"_index": index_name, "_id": identifier}}))
            handle.write("\n")
            handle.write(_compact_json(document))
            handle.write("\n")


def _write_embedded_bulk(
    path: Path,
    index_name: str,
    items: Sequence[tuple[str, dict[str, Any], str]],
    client: EmbeddingClient,
    *,
    batch_size: int,
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            vectors = client.embed_documents([text for _, _, text in batch])
            if len(vectors) != len(batch):
                raise ValueError(
                    "embedding client returned an unexpected vector count: "
                    f"expected {len(batch)}, got {len(vectors)}"
                )
            for (identifier, document, _), raw_vector in zip(batch, vectors):
                vector = normalize_embedding(raw_vector, dimension=client.dimension)
                handle.write(
                    _compact_json({"index": {"_index": index_name, "_id": identifier}})
                )
                handle.write("\n")
                handle.write(_compact_json({**document, "embedding": vector}))
                handle.write("\n")


def _write_text(path: Path, value: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def _write_json(path: Path, value: Any, *, overwrite: bool) -> None:
    _write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        overwrite=overwrite,
    )


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _index_name_part(value: str) -> str:
    part = _INDEX_PART_RE.sub("-", value.lower()).strip("-")
    if not part:
        raise ValueError("dictionary_version cannot produce an empty OpenSearch index name")
    return part[:180]


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _csv_values(path: Path, field: str) -> set[str]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return {row[field] for row in csv.DictReader(handle)}


def _bulk_ids(path: Path) -> set[str]:
    identifiers: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle):
            if line_number % 2 == 0:
                identifiers.add(json.loads(line)["index"]["_id"])
    return identifiers
