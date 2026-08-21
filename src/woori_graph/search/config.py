"""Configuration for the independently deployable graph-search pipeline."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_INDEX_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")


@dataclass(frozen=True)
class SearchPipelineConfig:
    artifact_root: Path
    env_file: Path | None
    opensearch_url: str
    entity_index: str
    relation_index: str
    postgres_dsn_env: str
    opensearch_username_env: str | None = None
    opensearch_password_env: str | None = None
    verify_tls: bool = True
    max_hops: int = 3
    entity_top_k: int = 8
    relation_top_k: int = 20
    max_neighbors_per_entity: int = 30
    path_beam_width: int = 80
    max_paths: int = 30
    evidence_per_relation: int = 3
    timeout_seconds: float = 180.0
    request_timeout_seconds: float = 30.0
    answer_enabled: bool = False
    answer_prompt: Path | None = None
    answer_max_paths: int = 5
    answer_max_evidence_items: int = 15
    answer_timeout_seconds: float = 45.0

    def validate(self) -> None:
        if not self.opensearch_url.startswith(("http://", "https://")):
            raise ValueError("services.opensearch_url must be an HTTP(S) URL")
        for name in (self.entity_index, self.relation_index):
            if not _INDEX_RE.fullmatch(name):
                raise ValueError(f"invalid OpenSearch index or alias name: {name!r}")
        if not 1 <= self.max_hops <= 3:
            raise ValueError("search.max_hops must be between 1 and 3")
        for field_name in (
            "entity_top_k",
            "relation_top_k",
            "max_neighbors_per_entity",
            "path_beam_width",
            "max_paths",
            "evidence_per_relation",
        ):
            if getattr(self, field_name) < 1:
                raise ValueError(f"search.{field_name} must be at least 1")
        if not 1 <= self.timeout_seconds <= 180:
            raise ValueError("search.timeout_seconds must be between 1 and 180")
        if not 1 <= self.request_timeout_seconds <= min(60, self.timeout_seconds):
            raise ValueError(
                "search.request_timeout_seconds must be between 1 and 60 and "
                "not exceed timeout_seconds"
            )
        if not self.postgres_dsn_env.strip():
            raise ValueError("services.postgres_dsn_env must be non-empty")
        if self.answer_enabled:
            if self.answer_prompt is None:
                raise ValueError("answer.prompt is required when answer.enabled is true")
            if not self.answer_prompt.is_file():
                raise FileNotFoundError(
                    f"answer.prompt does not exist: {self.answer_prompt}"
                )
        if self.answer_max_paths < 1:
            raise ValueError("answer.max_paths must be at least 1")
        if self.answer_max_evidence_items < 1:
            raise ValueError("answer.max_evidence_items must be at least 1")
        if not 1 <= self.answer_timeout_seconds <= min(60, self.timeout_seconds):
            raise ValueError(
                "answer.timeout_seconds must be between 1 and 60 and not exceed "
                "search.timeout_seconds"
            )

    def postgres_dsn(self) -> str:
        value = os.environ.get(self.postgres_dsn_env, "").strip()
        if not value:
            raise RuntimeError(
                f"PostgreSQL DSN environment variable is required: {self.postgres_dsn_env}"
            )
        return value

    def opensearch_credentials(self) -> tuple[str, str] | None:
        if not self.opensearch_username_env and not self.opensearch_password_env:
            return None
        if not self.opensearch_username_env or not self.opensearch_password_env:
            raise ValueError(
                "both services.opensearch_username_env and "
                "services.opensearch_password_env are required"
            )
        username = os.environ.get(self.opensearch_username_env, "")
        password = os.environ.get(self.opensearch_password_env, "")
        if not username or not password:
            raise RuntimeError("configured OpenSearch credential environment variables are empty")
        return username, password


def load_search_config(path: Path) -> SearchPipelineConfig:
    """Load TOML and resolve paths relative to the config file."""

    config_path = path.resolve()
    payload = tomllib.loads(config_path.read_text(encoding="utf-8"))
    base = config_path.parent
    paths = _section(payload, "paths")
    services = _section(payload, "services")
    search = _section(payload, "search")
    answer = _section(payload, "answer")

    def path_value(name: str, *, required: bool) -> Path | None:
        raw = paths.get(name)
        if raw in (None, ""):
            if required:
                raise ValueError(f"paths.{name} is required")
            return None
        result = Path(str(raw))
        return result if result.is_absolute() else (base / result).resolve()

    def answer_prompt_value() -> Path | None:
        raw = answer.get("prompt")
        if raw in (None, ""):
            return None
        result = Path(str(raw))
        return result if result.is_absolute() else (base / result).resolve()

    config = SearchPipelineConfig(
        artifact_root=path_value("artifact_root", required=True),  # type: ignore[arg-type]
        env_file=path_value("env_file", required=False),
        opensearch_url=str(services.get("opensearch_url", "")).rstrip("/"),
        entity_index=str(services.get("entity_index", "entities")),
        relation_index=str(services.get("relation_index", "relations")),
        postgres_dsn_env=str(services.get("postgres_dsn_env", "GRAPH_POSTGRES_DSN")),
        opensearch_username_env=_optional_string(
            services.get("opensearch_username_env")
        ),
        opensearch_password_env=_optional_string(
            services.get("opensearch_password_env")
        ),
        verify_tls=bool(services.get("verify_tls", True)),
        max_hops=int(search.get("max_hops", 3)),
        entity_top_k=int(search.get("entity_top_k", 8)),
        relation_top_k=int(search.get("relation_top_k", 20)),
        max_neighbors_per_entity=int(search.get("max_neighbors_per_entity", 30)),
        path_beam_width=int(search.get("path_beam_width", 80)),
        max_paths=int(search.get("max_paths", 30)),
        evidence_per_relation=int(search.get("evidence_per_relation", 3)),
        timeout_seconds=float(search.get("timeout_seconds", 180)),
        request_timeout_seconds=float(search.get("request_timeout_seconds", 30)),
        answer_enabled=bool(answer.get("enabled", False)),
        answer_prompt=answer_prompt_value(),
        answer_max_paths=int(answer.get("max_paths", 5)),
        answer_max_evidence_items=int(answer.get("max_evidence_items", 15)),
        answer_timeout_seconds=float(answer.get("timeout_seconds", 45)),
    )
    config.validate()
    if config.env_file is not None and not config.env_file.is_file():
        raise FileNotFoundError(f"paths.env_file does not exist: {config.env_file}")
    return config


def _section(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
