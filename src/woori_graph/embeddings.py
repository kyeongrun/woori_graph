"""Reusable OpenAI-compatible embedding client and graph text builders."""

from __future__ import annotations

import math
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx


class EmbeddingClient(Protocol):
    """Minimal interface accepted by storage and search workflows."""

    @property
    def model(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    api_key: str
    model: str
    dimension: int
    timeout_seconds: float = 120.0
    max_retries: int = 2
    batch_size: int = 64
    document_prefix: str = ""
    query_prefix: str = ""
    normalize: bool = True
    trust_env: bool = False
    local_only: bool = True

    @classmethod
    def from_env(cls) -> "EmbeddingConfig":
        missing = [
            key
            for key in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_DIMENSION")
            if not os.environ.get(key, "").strip()
        ]
        if missing:
            raise RuntimeError(
                "Embedding configuration is required for vector load: "
                + ", ".join(missing)
            )
        return cls(
            base_url=os.environ["EMBEDDING_BASE_URL"].strip(),
            api_key=os.environ.get("EMBEDDING_API_KEY", "local"),
            model=os.environ["EMBEDDING_MODEL"].strip(),
            dimension=int(os.environ["EMBEDDING_DIMENSION"]),
            timeout_seconds=float(os.environ.get("EMBEDDING_TIMEOUT_SECONDS", "120")),
            max_retries=int(os.environ.get("EMBEDDING_MAX_RETRIES", "2")),
            batch_size=int(os.environ.get("EMBEDDING_BATCH_SIZE", "64")),
            document_prefix=os.environ.get("EMBEDDING_DOCUMENT_PREFIX", ""),
            query_prefix=os.environ.get("EMBEDDING_QUERY_PREFIX", ""),
            normalize=_as_bool(os.environ.get("EMBEDDING_NORMALIZE", "true")),
            trust_env=_as_bool(os.environ.get("EMBEDDING_TRUST_ENV", "false")),
            local_only=_as_bool(os.environ.get("EMBEDDING_LOCAL_ONLY", "true")),
        )

    def validate(self) -> None:
        if not self.model:
            raise ValueError("embedding model must be non-empty")
        if not 1 <= self.dimension <= 16000:
            raise ValueError("embedding dimension must be between 1 and 16000")
        if self.batch_size < 1:
            raise ValueError("embedding batch size must be at least 1")
        if self.max_retries < 0:
            raise ValueError("embedding max retries must be zero or greater")
        if self.timeout_seconds <= 0:
            raise ValueError("embedding timeout must be greater than zero")


class OpenAICompatEmbeddingClient:
    """Call a local vLLM or another OpenAI-compatible embeddings endpoint."""

    def __init__(self, config: EmbeddingConfig):
        config.validate()
        self._validate_endpoint(config)
        self._config = config
        self._client = httpx.Client(
            timeout=config.timeout_seconds,
            trust_env=config.trust_env,
        )

    @property
    def model(self) -> str:
        return self._config.model

    @property
    def dimension(self) -> int:
        return self._config.dimension

    @property
    def batch_size(self) -> int:
        return self._config.batch_size

    def close(self) -> None:
        self._client.close()

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, prefix=self._config.document_prefix)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, prefix=self._config.query_prefix)

    def _embed(self, texts: Sequence[str], *, prefix: str) -> list[list[float]]:
        if not texts:
            return []
        prepared = [f"{prefix}{text}" for text in texts]
        if any(not text.strip() for text in prepared):
            raise ValueError("embedding input text must be non-empty")
        payload = {
            "model": self.model,
            "input": prepared,
            "encoding_format": "float",
        }
        last_error: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._client.post(
                    f"{self._config.base_url.rstrip('/')}/embeddings",
                    headers={"Authorization": f"Bearer {self._config.api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                vectors = _parse_embedding_response(
                    response.json(),
                    expected_count=len(prepared),
                    expected_dimension=self.dimension,
                    normalize=self._config.normalize,
                )
                return vectors
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt == self._config.max_retries:
                    break
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError("OpenAI-compatible embedding request failed") from last_error

    @staticmethod
    def _validate_endpoint(config: EmbeddingConfig) -> None:
        if not config.local_only:
            return
        parsed = urlparse(config.base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                "EMBEDDING_LOCAL_ONLY=true permits only loopback embedding endpoints. "
                "Use --allow-remote-embedding only for an explicitly approved private endpoint."
            )


def entity_embedding_text(record: Mapping[str, Any], *, alias_limit: int = 20) -> str:
    """Build bounded entity text while leaving full aliases available to BM25."""

    canonical_name = str(record["canonical_name"]).strip()
    aliases: list[str] = []
    for raw_alias in record.get("aliases", []):
        if isinstance(raw_alias, Mapping):
            alias = str(raw_alias.get("name", "")).strip()
        else:
            alias = str(raw_alias).strip()
        if alias and alias != canonical_name and alias not in aliases:
            aliases.append(alias)
        if len(aliases) >= alias_limit:
            break
    if not aliases:
        return canonical_name
    return f"{canonical_name}\naliases: {', '.join(aliases)}"


def relation_embedding_text(
    record: Mapping[str, Any],
    *,
    relation_type_name: str,
) -> str:
    """Represent one directed edge as a compact natural-language triple."""

    return " ".join(
        (
            str(record["source_name"]).strip(),
            relation_type_name.strip(),
            str(record["target_name"]).strip(),
        )
    )


def normalize_embedding(vector: Sequence[float], *, dimension: int) -> list[float]:
    if len(vector) != dimension:
        raise ValueError(
            f"embedding dimension mismatch: expected {dimension}, got {len(vector)}"
        )
    values = [float(value) for value in vector]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding contains a non-finite value")
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        raise ValueError("embedding must not be a zero vector")
    return [round(value / norm, 8) for value in values]


def _parse_embedding_response(
    payload: Mapping[str, Any],
    *,
    expected_count: int,
    expected_dimension: int,
    normalize: bool,
) -> list[list[float]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("embedding response must contain a data array")
    ordered: list[tuple[int, list[float]]] = []
    for item in data:
        if not isinstance(item, Mapping):
            raise ValueError("embedding response data items must be objects")
        index = item.get("index")
        vector = item.get("embedding")
        if not isinstance(index, int) or not isinstance(vector, list):
            raise ValueError("embedding response item requires index and embedding")
        values = [float(value) for value in vector]
        if normalize:
            values = normalize_embedding(values, dimension=expected_dimension)
        elif len(values) != expected_dimension:
            raise ValueError(
                "embedding dimension mismatch: "
                f"expected {expected_dimension}, got {len(values)}"
            )
        ordered.append((index, values))
    ordered.sort(key=lambda item: item[0])
    if [index for index, _ in ordered] != list(range(expected_count)):
        raise ValueError("embedding response indices are incomplete or duplicated")
    return [vector for _, vector in ordered]


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
