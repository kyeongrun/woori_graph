"""Composition root for the search pipeline; adapters remain replaceable."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..embeddings import EmbeddingConfig, OpenAICompatEmbeddingClient
from ..extraction import OpenAICompatClient, OpenAICompatConfig
from .answering import GroundedAnswerConfig, GroundedAnswerGenerator
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
        allow_remote_llm: bool = False,
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
        self.answer_client: OpenAICompatClient | None = None
        answer_generator = None
        answer_unavailable_reason = None
        if config.answer_enabled:
            assert config.answer_prompt is not None  # validated by SearchPipelineConfig
            llm_config = OpenAICompatConfig.from_env()
            llm_config = replace(
                llm_config,
                timeout_seconds=min(llm_config.timeout_seconds, config.answer_timeout_seconds),
                max_retries=0,
            )
            if allow_remote_llm:
                llm_config = replace(llm_config, local_only=False)
            try:
                self.answer_client = OpenAICompatClient(llm_config)
            except ValueError as exc:
                if "SVO_LOCAL_ONLY=true" not in str(exc):
                    raise
                answer_unavailable_reason = "llm_endpoint_not_permitted"
            else:
                answer_generator = GroundedAnswerGenerator(
                    self.answer_client,
                    GroundedAnswerConfig(
                        prompt_path=config.answer_prompt,
                        max_paths=config.answer_max_paths,
                        max_evidence_items=config.answer_max_evidence_items,
                        timeout_seconds=config.answer_timeout_seconds,
                    ),
                    model=llm_config.model,
                )
        self.pipeline = GraphSearchPipeline(
            self.candidate_repository,
            self.graph_repository,
            self.embedding_client,
            config,
            answer_generator=answer_generator,
            answer_unavailable_reason=answer_unavailable_reason,
        )
        self.config = config

    def close(self) -> None:
        if self.answer_client is not None:
            self.answer_client.close()
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
