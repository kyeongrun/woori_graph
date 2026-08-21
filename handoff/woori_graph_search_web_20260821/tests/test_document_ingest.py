import json
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from woori_graph.graph_build.ingest import (
    load_document_ingest_config,
    run_document_ingest,
)
from woori_graph.ids import stable_id
from woori_graph.jsonl import read_jsonl, write_jsonl


class ExtractionClient:
    def complete(self, prompt: str) -> str:
        assert "unit_text" in prompt
        return json.dumps(
            {
                "relations": [
                    {
                        "subject": "위원회",
                        "predicate": "제출한다",
                        "object": "보고서",
                    }
                ]
            },
            ensure_ascii=False,
        )


@pytest.fixture
def ingest_workspace():
    root = (
        Path(__file__).resolve().parent
        / "_generated_document_ingest_test"
        / uuid4().hex
    )
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _prepare_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    source_dir = tmp_path / "incoming"
    dictionary_dir = tmp_path / "dictionary"
    config_dir.mkdir()
    source_dir.mkdir()
    dictionary_dir.mkdir()
    (source_dir / "test.md").write_text(
        """---
제목: 테스트법
법령MST: '999999'
---
# 테스트법
##### 제1조(제출)
위원회는 보고서를 제출한다.
""",
        encoding="utf-8",
    )
    write_jsonl(
        dictionary_dir / "entities.jsonl",
        [
            {
                "canonical_name": "위원회",
                "aliases": [{"name": "위원회"}],
                "entity_id": stable_id("entity", "위원회"),
                "entity_type": "ORGANIZATION",
            },
            {
                "canonical_name": "보고서",
                "aliases": [{"name": "보고서"}],
                "entity_id": stable_id("entity", "보고서"),
                "entity_type": "CONCEPT",
            },
        ],
    )
    write_jsonl(
        dictionary_dir / "relations.jsonl",
        [
            {
                "canonical_name": "제출하다",
                "aliases": [{"name": "제출하다"}, {"name": "제출한다"}],
                "relation_type_id": stable_id("relation_type", "제출하다"),
                "polarity": "POSITIVE",
            }
        ],
    )
    config_path = config_dir / "ingest.toml"
    config_path.write_text(
        """[run]
id = "run-001"
dictionary_version = "v1"

[paths]
source = "../incoming"
artifact_root = "../artifacts"
entity_dictionary = "../dictionary/entities.jsonl"
relation_dictionary = "../dictionary/relations.jsonl"

[execution]
workers = 1
batch_size = 10

[storage]
build_load_files = false
age_graph_name = "svo"
""",
        encoding="utf-8",
    )
    return config_path


def test_document_ingest_resolves_config_paths_and_writes_standard_artifacts(
    ingest_workspace: Path,
) -> None:
    config = load_document_ingest_config(_prepare_config(ingest_workspace))

    manifest = run_document_ingest(config, ExtractionClient())

    assert config.source == (ingest_workspace / "incoming").resolve()
    assert manifest["run_id"] == "run-001"
    assert manifest["config"]["path"] == "ingest.toml"
    assert manifest["checks"]["semantic_units_present"] is True
    assert manifest["checks"]["all_units_extracted"] is True
    assert (config.work_dir / "01_semantic_units.jsonl").is_file()
    assert (config.work_dir / "02_raw_svo.jsonl").is_file()
    assert (config.work_dir / "source_manifest.jsonl").is_file()
    assert (config.final_dir / "ingestion_manifest.json").is_file()

    entities = list(read_jsonl(config.final_dir / "entities.jsonl"))
    assert {item["canonical_name"] for item in entities} == {"위원회", "보고서"}
    assert all("dictionary_match" not in item for item in entities)
    assert all("dictionary_version" not in item for item in entities)
    assert all("mention_count" not in item for item in entities)

    relations = list(read_jsonl(config.final_dir / "relations.jsonl"))
    assert relations[0]["relation_type_id"] == stable_id("relation_type", "제출하다")
    assert relations[0]["source_entity_id"] == stable_id("entity", "위원회")
    assert relations[0]["target_entity_id"] == stable_id("entity", "보고서")


def test_document_ingest_config_is_independent_of_current_directory(
    ingest_workspace: Path, monkeypatch
) -> None:
    config_path = _prepare_config(ingest_workspace)
    elsewhere = ingest_workspace / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_document_ingest_config(config_path)

    assert config.artifact_root == (ingest_workspace / "artifacts").resolve()
    assert config.entity_dictionary == (
        ingest_workspace / "dictionary" / "entities.jsonl"
    ).resolve()
