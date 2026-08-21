"""Config-driven, reusable ingestion workflow for new Markdown documents."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ..documents import discover_markdown
from ..embeddings import EmbeddingClient
from ..entity_typing import ENTITY_TYPE_SET
from ..extraction import CompletionClient, extract_units, load_prompt
from ..graph_mapping import collect_unmapped_predicates, map_raw_svo_to_graph
from ..jsonl import append_jsonl, read_jsonl, write_jsonl
from ..prompting import DEFAULT_PROMPT_ROOT
from ..storage import build_storage_load_files
from .pipeline import GraphBuildPipeline
from .relation_mapper import propose_forced_relation_overrides


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class DocumentIngestConfig:
    """Resolved paths and execution values for one document-ingest run."""

    config_path: Path
    run_id: str
    dictionary_version: str
    source: Path
    artifact_root: Path
    entity_dictionary: Path
    relation_dictionary: Path
    prompt_file: Path
    env_file: Path | None = None
    load_root: Path | None = None
    workers: int = 4
    batch_size: int = 200
    relation_workers: int = 4
    relation_batch_size: int = 40
    build_load_files: bool = False
    age_graph_name: str = "svo"

    @property
    def run_dir(self) -> Path:
        return self.artifact_root / self.run_id

    @property
    def work_dir(self) -> Path:
        return self.run_dir / "work"

    @property
    def final_dir(self) -> Path:
        return self.run_dir / "final"


def load_document_ingest_config(config_path: Path) -> DocumentIngestConfig:
    """Load TOML and resolve every relative path from the TOML directory."""

    resolved_config = config_path.resolve()
    with resolved_config.open("rb") as handle:
        payload = tomllib.load(handle)
    run = _required_table(payload, "run")
    paths = _required_table(payload, "paths")
    run_id = _required_text(run, "id")
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(
            "run.id must start with an alphanumeric character and contain only "
            "letters, digits, dot, underscore, or hyphen"
        )
    dictionary_version = _required_text(run, "dictionary_version")
    base = resolved_config.parent

    def path_value(name: str, *, required: bool = True) -> Path | None:
        value = paths.get(name)
        if value is None:
            if required:
                raise ValueError(f"paths.{name} is required")
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"paths.{name} must be a non-empty string")
        path = Path(value)
        return (base / path).resolve() if not path.is_absolute() else path.resolve()

    execution = payload.get("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("execution must be a TOML table")
    storage = payload.get("storage", {})
    if not isinstance(storage, dict):
        raise ValueError("storage must be a TOML table")
    prompt_file = path_value("prompt", required=False)
    if prompt_file is None:
        prompt_file = (DEFAULT_PROMPT_ROOT / "raw_svo_extract.ko.md").resolve()
    build_load_files_value = storage.get("build_load_files", False)
    if not isinstance(build_load_files_value, bool):
        raise ValueError("storage.build_load_files must be true or false")
    load_root = path_value("load_root", required=build_load_files_value)

    config = DocumentIngestConfig(
        config_path=resolved_config,
        run_id=run_id,
        dictionary_version=dictionary_version,
        source=path_value("source"),
        artifact_root=path_value("artifact_root"),
        entity_dictionary=path_value("entity_dictionary"),
        relation_dictionary=path_value("relation_dictionary"),
        prompt_file=prompt_file,
        env_file=path_value("env_file", required=False),
        load_root=load_root,
        workers=_positive_int(execution, "workers", 4),
        batch_size=_positive_int(execution, "batch_size", 200),
        relation_workers=_positive_int(execution, "relation_workers", 4),
        relation_batch_size=_positive_int(execution, "relation_batch_size", 40),
        build_load_files=build_load_files_value,
        age_graph_name=str(storage.get("age_graph_name", "svo")).strip(),
    )
    _validate_ingest_inputs(config)
    return config


def run_document_ingest(
    config: DocumentIngestConfig,
    client: CompletionClient,
    *,
    embedding_client: EmbeddingClient | None = None,
    embedding_batch_size: int = 64,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Run segment -> extract -> dictionary map -> final/load artifacts."""

    manifest_path = config.final_dir / "ingestion_manifest.json"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite ingestion run: {config.run_dir}")
    config.work_dir.mkdir(parents=True, exist_ok=True)
    config.final_dir.mkdir(parents=True, exist_ok=True)

    source_manifest = _build_source_manifest(config.source)
    write_jsonl(
        config.work_dir / "source_manifest.jsonl",
        source_manifest,
        overwrite=overwrite,
    )

    pipeline = GraphBuildPipeline()
    units = pipeline.segment_path(config.source)
    write_jsonl(
        config.work_dir / "01_semantic_units.jsonl",
        (unit.to_dict() for unit in units),
        overwrite=overwrite,
    )

    prompt = load_prompt(config.prompt_file)
    raw_path = config.work_dir / "02_raw_svo.jsonl"
    error_path = config.work_dir / "02_raw_svo.errors.jsonl"
    write_jsonl(raw_path, [], overwrite=overwrite)
    write_jsonl(error_path, [], overwrite=overwrite)
    raw_records: list[dict[str, Any]] = []
    extraction_errors: list[dict[str, Any]] = []
    for start in range(0, len(units), config.batch_size):
        records, errors = extract_units(
            units[start : start + config.batch_size],
            client,
            workers=config.workers,
            prompt_template=prompt,
        )
        append_jsonl(raw_path, records)
        append_jsonl(error_path, errors)
        raw_records.extend(records)
        extraction_errors.extend(errors)
    if extraction_errors:
        raise RuntimeError(
            f"SVO extraction failed for {len(extraction_errors)} semantic units; "
            f"see {error_path}"
        )

    entity_dictionary = list(read_jsonl(config.entity_dictionary))
    relation_dictionary = list(read_jsonl(config.relation_dictionary))
    missing_predicates = collect_unmapped_predicates(raw_records, relation_dictionary)
    relation_overrides: dict[str, str] = {}
    relation_errors: list[dict[str, Any]] = []
    if missing_predicates:
        relation_overrides, relation_errors = propose_forced_relation_overrides(
            missing_predicates,
            relation_dictionary,
            client,
            batch_size=config.relation_batch_size,
            workers=config.relation_workers,
        )
    write_jsonl(
        config.work_dir / "04_relation_mapping_errors.jsonl",
        relation_errors,
        overwrite=overwrite,
    )
    if relation_errors or set(missing_predicates) != set(relation_overrides):
        unresolved = sorted(set(missing_predicates) - set(relation_overrides))
        raise RuntimeError(
            f"Closed relation mapping failed for {len(unresolved)} predicates; "
            f"see {config.work_dir / '04_relation_mapping_errors.jsonl'}"
        )

    bundle = map_raw_svo_to_graph(
        raw_records,
        entity_dictionary,
        relation_dictionary,
        dictionary_version=config.dictionary_version,
        relation_overrides=relation_overrides,
    )
    write_jsonl(
        config.work_dir / "03_entity_mapping_results.jsonl",
        bundle.entity_mapping_results,
        overwrite=overwrite,
    )
    write_jsonl(
        config.work_dir / "04_relation_mapping_results.jsonl",
        bundle.relation_mapping_results,
        overwrite=overwrite,
    )
    _write_final_bundle(config.final_dir, bundle, overwrite=overwrite)

    audit = _audit_bundle(units, raw_records, bundle)
    _write_json(config.final_dir / "load_audit.json", audit, overwrite=overwrite)
    if not audit["passed"]:
        raise RuntimeError(f"Ingestion audit failed; see {config.final_dir / 'load_audit.json'}")

    load_manifest: dict[str, Any] | None = None
    if config.build_load_files:
        assert config.load_root is not None
        if embedding_client is None:
            raise RuntimeError(
                "storage.build_load_files=true requires an embedding client; "
                "configure EMBEDDING_BASE_URL, EMBEDDING_MODEL, and EMBEDDING_DIMENSION"
            )
        load_manifest = build_storage_load_files(
            raw_records,
            entity_dictionary,
            relation_dictionary,
            dictionary_version=config.dictionary_version,
            output_dir=config.load_root / config.run_id,
            age_graph_name=config.age_graph_name,
            relation_overrides=relation_overrides,
            embedding_client=embedding_client,
            embedding_batch_size=embedding_batch_size,
            overwrite=overwrite,
        )
        if not load_manifest["passed"]:
            raise RuntimeError("Storage load-file audit failed")

    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    manifest = {
        "run_id": config.run_id,
        "dictionary_version": config.dictionary_version,
        "created_at": created_at,
        "config": _file_descriptor(config.config_path, config.config_path.parent),
        "prompt": _file_descriptor(config.prompt_file, config.config_path.parent),
        "dictionaries": {
            "entity": _file_descriptor(
                config.entity_dictionary, config.config_path.parent
            ),
            "relation": _file_descriptor(
                config.relation_dictionary, config.config_path.parent
            ),
        },
        "execution": {
            "workers": config.workers,
            "batch_size": config.batch_size,
            "relation_workers": config.relation_workers,
            "relation_batch_size": config.relation_batch_size,
        },
        "source_manifest": "work/source_manifest.jsonl",
        "counts": audit["counts"],
        "checks": audit["checks"],
        "load_files": (
            str((config.load_root / config.run_id).resolve())
            if load_manifest is not None and config.load_root is not None
            else None
        ),
    }
    output_files = sorted(
        path
        for path in config.run_dir.rglob("*")
        if path.is_file() and path != manifest_path
    )
    manifest["outputs"] = [
        {
            "path": path.relative_to(config.run_dir).as_posix(),
            "sha256": _sha256(path),
            "records": _jsonl_count(path) if path.suffix == ".jsonl" else None,
        }
        for path in output_files
    ]
    _write_json(manifest_path, manifest, overwrite=overwrite)
    return manifest


def _write_final_bundle(final_dir: Path, bundle: Any, *, overwrite: bool) -> None:
    write_jsonl(final_dir / "documents.jsonl", bundle.documents, overwrite=overwrite)
    write_jsonl(
        final_dir / "entities.jsonl",
        (_without_work_fields(item) for item in bundle.entities),
        overwrite=overwrite,
    )
    write_jsonl(
        final_dir / "relation_types.jsonl",
        (_without_work_fields(item) for item in bundle.relation_types),
        overwrite=overwrite,
    )
    write_jsonl(
        final_dir / "relations.jsonl",
        (_without_work_fields(item) for item in bundle.relations),
        overwrite=overwrite,
    )
    write_jsonl(
        final_dir / "unmapped_entity_candidates.jsonl",
        bundle.unmapped_entities,
        overwrite=overwrite,
    )


def _without_work_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key not in {"dictionary_match", "dictionary_version", "mention_count"}
    }


def _audit_bundle(units: list[Any], raw_records: list[dict[str, Any]], bundle: Any) -> dict[str, Any]:
    unit_ids = {unit.semantic_unit_id for unit in units}
    raw_unit_ids = {item["semantic_unit_id"] for item in raw_records}
    entity_ids = {item["entity_id"] for item in bundle.entities}
    relation_type_ids = {item["relation_type_id"] for item in bundle.relation_types}
    relation_ids = {item["relation_id"] for item in bundle.relations}
    checks = {
        "semantic_units_present": bool(units),
        "all_units_extracted": len(raw_records) == len(units) and raw_unit_ids == unit_ids,
        "all_entity_ids_are_uuid": all(_is_uuid(item) for item in entity_ids),
        "all_relation_ids_are_uuid": all(_is_uuid(item) for item in relation_ids),
        "all_relation_endpoints_exist": all(
            item["source_entity_id"] in entity_ids and item["target_entity_id"] in entity_ids
            for item in bundle.relations
        ),
        "all_relation_types_exist": all(
            item["relation_type_id"] in relation_type_ids for item in bundle.relations
        ),
        "all_entity_types_valid": all(
            item.get("entity_type") in ENTITY_TYPE_SET for item in bundle.entities
        ),
        "all_relations_mapped": all(
            item.get("mapping_status") in {"dictionary_alias", "forced_dictionary_mapping"}
            for item in bundle.relation_mapping_results
        ),
    }
    return {
        "passed": all(checks.values()),
        "counts": {
            "source_documents": len({item["document_id"] for item in raw_records}),
            "semantic_units": len(units),
            "raw_svo_records": len(raw_records),
            "raw_relations": sum(len(item.get("relations", [])) for item in raw_records),
            "entities": len(bundle.entities),
            "entity_types": {
                entity_type: sum(
                    item.get("entity_type") == entity_type for item in bundle.entities
                )
                for entity_type in sorted(ENTITY_TYPE_SET)
            },
            "relation_types": len(bundle.relation_types),
            "relations": len(bundle.relations),
            "unmapped_entity_candidates": len(bundle.unmapped_entities),
        },
        "checks": checks,
    }


def _build_source_manifest(source: Path) -> list[dict[str, Any]]:
    root = source if source.is_dir() else source.parent
    records = []
    collected_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for path in discover_markdown(source):
        records.append(
            {
                "source_document_key": path.relative_to(root).as_posix(),
                "source_path": path.relative_to(root).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
                "collected_at": collected_at,
            }
        )
    return records


def _validate_ingest_inputs(config: DocumentIngestConfig) -> None:
    for name, path in (
        ("paths.source", config.source),
        ("paths.entity_dictionary", config.entity_dictionary),
        ("paths.relation_dictionary", config.relation_dictionary),
        ("paths.prompt", config.prompt_file),
    ):
        if not path.exists():
            raise FileNotFoundError(f"{name} does not exist: {path}")
    if config.env_file is not None and not config.env_file.is_file():
        raise FileNotFoundError(f"paths.env_file does not exist: {config.env_file}")
    if config.build_load_files and not config.age_graph_name:
        raise ValueError("storage.age_graph_name must be non-empty")


def _required_table(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a TOML table")
    return value


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _positive_int(payload: dict[str, Any], key: str, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"execution.{key} must be a positive integer")
    return value


def _file_descriptor(path: Path, relative_root: Path) -> dict[str, Any]:
    try:
        display_path = Path(os.path.relpath(path, relative_root)).as_posix()
    except ValueError:
        display_path = str(path)
    return {"path": display_path, "sha256": _sha256(path)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl_count(path: Path) -> int:
    return sum(1 for _ in read_jsonl(path))


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _write_json(path: Path, value: dict[str, Any], *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
