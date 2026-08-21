"""Composition root for the search pipeline; adapters remain replaceable."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..embeddings import EmbeddingConfig, OpenAICompatEmbeddingClient
from .artifacts import write_search_artifacts
from .config import SearchPipelineConfig
from .models import SearchResult
from .pipeline import GraphSearchPipeline
from .repositories import OpenSearchCandidateRepository, PostgresGraphRepository


class SearchApplication:
    """Reusable application service suitable for a CLI or an internal API."""

    def __init__(
        self,
        config: SearchPipelineConfig,
        *,
        allow_remote_embedding: bool = False,
    ):
        embedding_config = EmbeddingConfig.from_env()
        embedding_config = replace(
            embedding_config,
            timeout_seconds=min(
                embedding_config.timeout_seconds,
                config.request_timeout_seconds,
            ),
            max_retries=min(embedding_config.max_retries, 1),
        )
        if allow_remote_embedding:
            embedding_config = replace(embedding_config, local_only=False)
        self.embedding_client = OpenAICompatEmbeddingClient(embedding_config)
        self.candidate_repository = OpenSearchCandidateRepository(
            config.opensearch_url,
            entity_index=config.entity_index,
            relation_index=config.relation_index,
            timeout_seconds=config.request_timeout_seconds,
            credentials=config.opensearch_credentials(),
            verify_tls=config.verify_tls,
        )
        self.graph_repository = PostgresGraphRepository(
            config.postgres_dsn(),
            statement_timeout_seconds=config.request_timeout_seconds,
        )
        self.pipeline = GraphSearchPipeline(
            self.candidate_repository,
            self.graph_repository,
            self.embedding_client,
            config,
        )
        self.config = config

    def close(self) -> None:
        self.embedding_client.close()
        self.candidate_repository.close()
        self.graph_repository.close()

    def __enter__(self) -> "SearchApplication":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def search(self, query: str) -> SearchResult:
        return self.pipeline.search(query)

    def search_and_write(
        self,
        query: str,
        *,
        config_path: Path,
        run_id: str | None = None,
        overwrite: bool = False,
    ) -> tuple[SearchResult, dict[str, object]]:
        result = self.search(query)
        manifest = write_search_artifacts(
            result,
            config=self.config,
            config_path=config_path,
            embedding_model=self.embedding_client.model,
            embedding_dimension=self.embedding_client.dimension,
            run_id=run_id,
            overwrite=overwrite,
        )
        return result, manifest

