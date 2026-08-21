import csv
import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from woori_graph.ids import stable_id
from woori_graph.entity_typing import EntityTypeValidationError
from woori_graph.storage import (
    build_storage_load_files,
    relation_overrides_from_records,
)


class FakeEmbeddingClient:
    model = "fake-embedding"
    dimension = 3

    def __init__(self):
        self.inputs = []

    def embed_documents(self, texts):
        self.inputs.extend(texts)
        return [[1.0, 2.0, 2.0] for _ in texts]


@pytest.fixture
def storage_workspace():
    root = Path(__file__).resolve().parent / "_generated_storage_test" / uuid4().hex
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _fixture_records():
    source_id = stable_id("entity", "금융감독원")
    target_id = stable_id("entity", "벌금")
    relation_type_id = stable_id("relation_type", "부과하다")
    raw = [
        {
            "semantic_unit_id": stable_id("semantic_unit", "doc-1", "unit-1"),
            "document_id": stable_id("document", "doc-1"),
            "document_title": "테스트법",
            "source_path": "laws/test.md",
            "source_ref": {"article": "제1조", "paragraph": 1, "item_path": []},
            "context_text": "제1조(부과) 금융감독원은 필요한 처분을 할 수 있다.",
            "unit_text": "금감원은 벌금을 부과한다.",
            "unit_kind": "paragraph",
            "relations": [
                {
                    "relation_mention_id": stable_id("relation_mention", "mention-1"),
                    "subject": "금감원",
                    "predicate": "부과한다",
                    "object": "벌금",
                }
            ],
        }
    ]
    entities = [
        {
            "canonical_name": "금융감독원",
            "aliases": [{"name": "금융감독원"}, {"name": "금감원"}],
            "entity_id": source_id,
            "entity_type": "ORGANIZATION",
        },
        {
            "canonical_name": "벌금",
            "aliases": [{"name": "벌금"}],
            "entity_id": target_id,
            "entity_type": "CONCEPT",
        },
    ]
    relations = [
        {
            "canonical_name": "부과하다",
            "aliases": [{"name": "부과하다"}, {"name": "부과한다"}],
            "relation_type_id": relation_type_id,
            "polarity": "POSITIVE",
        }
    ]
    return raw, entities, relations


def test_build_storage_load_files_reuses_uuid_strings_across_stores(
    storage_workspace,
) -> None:
    raw, entities, relations = _fixture_records()
    output = storage_workspace / "release_v1"

    manifest = build_storage_load_files(
        raw,
        entities,
        relations,
        dictionary_version="v1",
        output_dir=output,
        age_graph_name="svo",
        created_at="2026-08-20T12:00:00+09:00",
    )

    assert manifest["passed"] is True
    assert manifest["counts"] == {
        "documents": 1,
        "semantic_units": 1,
        "entities": 2,
        "relation_types": 1,
        "relations": 1,
        "evidence": 1,
        "entity_mapping_results": 2,
        "relation_mapping_results": 1,
        "unmapped_entities": 0,
    }

    with (output / "rdb" / "entities.csv").open(encoding="utf-8", newline="") as handle:
        rdb_entity_ids = {row["entity_id"] for row in csv.DictReader(handle)}
    age_entity_ids = set()
    for path in (output / "age").glob("vertices_*.csv"):
        with path.open(encoding="utf-8", newline="") as handle:
            age_entity_ids.update(row["uuid"] for row in csv.DictReader(handle))
    bulk_lines = (output / "opensearch" / "entities.bulk.ndjson").read_text(
        encoding="utf-8"
    ).splitlines()
    opensearch_entity_ids = {
        json.loads(line)["index"]["_id"] for line in bulk_lines[::2]
    }

    assert rdb_entity_ids == age_entity_ids == opensearch_entity_ids
    assert "SET n.id = n.uuid REMOVE n.uuid" in (
        output / "age" / "load.sql"
    ).read_text(encoding="utf-8")
    assert "REMOVE n.__id__" in (
        output / "age" / "load.sql"
    ).read_text(encoding="utf-8")
    age_sql = (output / "age" / "load.sql").read_text(encoding="utf-8")
    assert "create_vlabel('svo', 'ORGANIZATION')" in age_sql
    assert "create_vlabel('svo', 'PERSON')" in age_sql
    assert "create_vlabel('svo', 'LEGAL_INSTRUMENT')" in age_sql
    assert "create_vlabel('svo', 'CONCEPT')" in age_sql
    assert "create_vlabel('svo', 'OTHER')" in age_sql
    edge_csv = next((output / "age").glob("edges_*.csv"))
    with edge_csv.open(encoding="utf-8", newline="") as handle:
        edge = next(csv.DictReader(handle))
    assert edge["start_vertex_type"] == "ORGANIZATION"
    assert edge["end_vertex_type"] == "CONCEPT"
    assert "/runtime/load/release_v1/rdb/entities.csv" in (
        output / "rdb" / "load.sql"
    ).read_text(encoding="utf-8")
    with (output / "rdb" / "semantic_units.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        source = next(csv.DictReader(handle))
    assert source["unit_text"] == "금감원은 벌금을 부과한다."
    assert source["context_text"].startswith("제1조")
    schema_sql = (output / "rdb" / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS graph.semantic_unit" in schema_sql
    assert "relation_evidence_semantic_unit_fk" in schema_sql


def test_storage_load_files_keeps_age_properties_minimal(
    storage_workspace,
) -> None:
    raw, entities, relations = _fixture_records()
    output = storage_workspace / "release_age"

    build_storage_load_files(
        raw,
        entities,
        relations,
        dictionary_version="v1",
        output_dir=output,
        age_graph_name="svo",
        created_at="2026-08-20T12:00:00+09:00",
    )

    with (output / "age" / "vertices_organization.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        assert next(csv.reader(handle)) == ["id", "uuid"]

    edge_file = next((output / "age").glob("edges_*.csv"))
    with edge_file.open(encoding="utf-8", newline="") as handle:
        edge = next(csv.DictReader(handle))
    assert set(edge) == {
        "start_id",
        "start_vertex_type",
        "end_id",
        "end_vertex_type",
        "uuid",
        "source_name",
        "target_name",
    }

def test_storage_load_files_rejects_missing_entity_type(
    storage_workspace,
) -> None:
    raw, entities, relations = _fixture_records()
    entities[0].pop("entity_type")
    output = storage_workspace / "release_v1"

    with pytest.raises(EntityTypeValidationError, match="classify-entity-types"):
        build_storage_load_files(
            raw,
            entities,
            relations,
            dictionary_version="v1",
            output_dir=output,
            age_graph_name="svo",
            created_at="2026-08-20T12:00:00+09:00",
        )


def test_storage_load_files_keep_explicit_entity_types(storage_workspace) -> None:
    raw, entities, relations = _fixture_records()
    output = storage_workspace / "release_v1"

    build_storage_load_files(
        raw,
        entities,
        relations,
        dictionary_version="v1",
        output_dir=output,
        age_graph_name="svo",
        created_at="2026-08-20T12:00:00+09:00",
    )

    records = [
        json.loads(line)
        for line in (output / "records" / "entities.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert {record["entity_type"] for record in records} == {
        "ORGANIZATION",
        "CONCEPT",
    }

    schema_sql = (output / "rdb" / "schema.sql").read_text(encoding="utf-8")
    assert "CREATE SCHEMA IF NOT EXISTS graph" in schema_sql
    assert "woori_graph." not in schema_sql

    manifest = json.loads(
        (output / "load_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["age_graph_name"] == "svo"
    assert manifest["rdb"] == {
        "database": "graphdb",
        "schema": "graph",
        "tables": [
            "document",
            "semantic_unit",
            "entity",
            "relation_type",
            "relation",
            "relation_evidence",
        ],
    }
    assert manifest["opensearch"]["entity_alias"] == "entities"
    assert manifest["opensearch"]["relation_alias"] == "relations"

    entity_header = (output / "rdb" / "entities.csv").read_text(
        encoding="utf-8"
    ).splitlines()[0]
    assert entity_header == "entity_id,canonical_name,entity_type,aliases_json"
    assert "dictionary_version" not in (
        output / "rdb" / "schema.sql"
    ).read_text(encoding="utf-8")
    assert "load_release" not in schema_sql

    entity_mapping = json.loads(
        (output / "opensearch" / "entities.mapping.json").read_text(encoding="utf-8")
    )
    entity_fields = entity_mapping["mappings"]["properties"]
    assert "dictionary_match" not in entity_fields
    assert "dictionary_version" not in entity_fields
    assert "mention_count" not in entity_fields


def test_storage_load_files_rejects_source_less_raw_records(storage_workspace) -> None:
    raw, entities, relations = _fixture_records()
    raw[0].pop("unit_text")

    with pytest.raises(ValueError, match="missing source fields.*unit_text"):
        build_storage_load_files(
            raw,
            entities,
            relations,
            dictionary_version="v1",
            output_dir=storage_workspace / "release_missing_source",
        )


def test_relation_override_records_reject_conflicts() -> None:
    with pytest.raises(ValueError, match="conflicting relation override"):
        relation_overrides_from_records(
            [
                {"raw_predicate": "한다", "relation_type_id": "first"},
                {"raw_predicate": "한다", "relation_type_id": "second"},
            ]
        )


def test_age_graph_name_is_restricted(storage_workspace) -> None:
    raw, entities, relations = _fixture_records()

    with pytest.raises(ValueError, match="age_graph_name"):
        build_storage_load_files(
            raw,
            entities,
            relations,
            dictionary_version="v1",
            output_dir=storage_workspace / "release_v1",
            age_graph_name="Invalid-Graph",
        )


def test_storage_load_files_adds_open_search_vectors_automatically(
    storage_workspace,
) -> None:
    raw, entities, relations = _fixture_records()
    output = storage_workspace / "release_vectors"
    client = FakeEmbeddingClient()

    manifest = build_storage_load_files(
        raw,
        entities,
        relations,
        dictionary_version="v1",
        output_dir=output,
        embedding_client=client,
        embedding_batch_size=1,
        created_at="2026-08-20T12:00:00+09:00",
    )

    assert manifest["opensearch"]["embedding"] == {
        "enabled": True,
        "field": "embedding",
        "model": "fake-embedding",
        "dimension": 3,
        "space_type": "cosinesimil",
        "engine": "lucene",
    }
    mapping = json.loads(
        (output / "opensearch" / "entities.mapping.json").read_text(encoding="utf-8")
    )
    assert mapping["settings"]["index"]["knn"] is True
    assert mapping["mappings"]["properties"]["embedding"] == {
        "type": "knn_vector",
        "dimension": 3,
        "method": {
            "name": "hnsw",
            "engine": "lucene",
            "space_type": "cosinesimil",
            "parameters": {"ef_construction": 100, "m": 16},
        },
    }
    bulk_lines = (output / "opensearch" / "entities.bulk.ndjson").read_text(
        encoding="utf-8"
    ).splitlines()
    documents = [json.loads(line) for line in bulk_lines[1::2]]
    assert all(document["embedding"] == [0.33333333, 0.66666667, 0.66666667] for document in documents)
    assert len(client.inputs) == 3
