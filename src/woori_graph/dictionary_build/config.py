"""Configuration for the context-complete dictionary build pipeline."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class DictionaryBuildConfig:
    config_path: Path
    dictionary_version: str
    source: Path
    artifact_root: Path
    context_prompt: Path
    raw_svo_prompt: Path
    env_file: Path | None
    context_workers: int
    svo_workers: int
    batch_size: int
    relation_max_types: int
    relation_polarity_strategy: str
    postgres_schema: str
    age_graph: str
    opensearch_entity_alias: str
    opensearch_relation_alias: str

    @property
    def run_dir(self) -> Path:
        return self.artifact_root / self.dictionary_version

    @property
    def work_dir(self) -> Path:
        return self.run_dir / "work"

    @property
    def final_dir(self) -> Path:
        return self.run_dir / "final"


def load_dictionary_build_config(config_path: Path) -> DictionaryBuildConfig:
    resolved_config = config_path.resolve()
    with resolved_config.open("rb") as handle:
        payload = tomllib.load(handle)
    run = _table(payload, "run")
    paths = _table(payload, "paths")
    execution = _table(payload, "execution")
    relations = _table(payload, "relations")
    storage = _table(payload, "storage")

    version = _text(run, "dictionary_version")
    if not _VERSION_RE.fullmatch(version):
        raise ValueError("run.dictionary_version contains unsupported characters")
    base = resolved_config.parent

    def resolve_path(name: str, *, required: bool = True) -> Path | None:
        value = paths.get(name)
        if value is None:
            if required:
                raise ValueError(f"paths.{name} is required")
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"paths.{name} must be a non-empty string")
        path = Path(value)
        return (base / path).resolve() if not path.is_absolute() else path.resolve()

    config = DictionaryBuildConfig(
        config_path=resolved_config,
        dictionary_version=version,
        source=resolve_path("source"),
        artifact_root=resolve_path("artifact_root"),
        context_prompt=resolve_path("context_prompt"),
        raw_svo_prompt=resolve_path("raw_svo_prompt"),
        env_file=resolve_path("env_file", required=False),
        context_workers=_positive_int(execution, "context_workers", 48),
        svo_workers=_positive_int(execution, "svo_workers", 48),
        batch_size=_positive_int(execution, "batch_size", 200),
        relation_max_types=_positive_int(relations, "max_types", 100),
        relation_polarity_strategy=_text(relations, "polarity_strategy"),
        postgres_schema=_text(storage, "postgres_schema"),
        age_graph=_text(storage, "age_graph"),
        opensearch_entity_alias=_text(storage, "opensearch_entity_alias"),
        opensearch_relation_alias=_text(storage, "opensearch_relation_alias"),
    )
    _validate(config)
    return config


def _validate(config: DictionaryBuildConfig) -> None:
    for path, label in (
        (config.source, "paths.source"),
        (config.context_prompt, "paths.context_prompt"),
        (config.raw_svo_prompt, "paths.raw_svo_prompt"),
    ):
        if not path.exists():
            raise FileNotFoundError(f"{label} does not exist: {path}")
    if config.env_file is not None and not config.env_file.exists():
        raise FileNotFoundError(f"paths.env_file does not exist: {config.env_file}")
    if config.relation_max_types > 100:
        raise ValueError("relations.max_types must be 100 or less")
    if config.relation_polarity_strategy != "separate_canonical_types":
        raise ValueError(
            "relations.polarity_strategy must be separate_canonical_types"
        )
    if config.postgres_schema != "graph_v2":
        raise ValueError("storage.postgres_schema must be graph_v2")
    if config.age_graph != "svo_v2":
        raise ValueError("storage.age_graph must be svo_v2")
    if config.opensearch_entity_alias != "entities_v2":
        raise ValueError("storage.opensearch_entity_alias must be entities_v2")
    if config.opensearch_relation_alias != "relations_v2":
        raise ValueError("storage.opensearch_relation_alias must be relations_v2")


def _table(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


def _text(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _positive_int(payload: dict[str, Any], name: str, default: int) -> int:
    value = payload.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value
