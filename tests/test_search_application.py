from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from woori_graph.extraction import OpenAICompatConfig
from woori_graph.search import application as application_module
from woori_graph.search.application import SearchApplication
from woori_graph.search.config import SearchPipelineConfig


class _Closeable:
    model = "fake-embedding"
    dimension = 3

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def close(self) -> None:
        pass


def test_application_starts_when_configured_llm_endpoint_is_not_permitted(
    monkeypatch,
) -> None:
    tmp_path = _workspace_tmp()
    prompt = tmp_path / "grounded_answer.ko.md"
    prompt.write_text("근거만 사용", encoding="utf-8")
    config = SearchPipelineConfig(
        artifact_root=tmp_path / "artifacts",
        env_file=None,
        opensearch_url="http://127.0.0.1:19200",
        entity_index="entities",
        relation_index="relations",
        postgres_dsn_env="TEST_SEARCH_DSN",
        answer_enabled=True,
        answer_prompt=prompt,
    )
    monkeypatch.setenv("TEST_SEARCH_DSN", "postgresql://test")
    monkeypatch.setenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "fake-embedding")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "3")
    monkeypatch.setattr(application_module, "OpenAICompatEmbeddingClient", _Closeable)
    monkeypatch.setattr(application_module, "OpenSearchCandidateRepository", _Closeable)
    monkeypatch.setattr(application_module, "PostgresGraphRepository", _Closeable)
    monkeypatch.setattr(
        application_module.OpenAICompatConfig,
        "from_env",
        lambda: OpenAICompatConfig(
            base_url="http://10.0.0.5:8000/v1",
            api_key="local",
            model="internal-llm",
        ),
    )

    application = SearchApplication(config)
    try:
        assert application.pipeline._answer_unavailable_reason == "llm_endpoint_not_permitted"
    finally:
        application.close()


def _workspace_tmp() -> Path:
    path = Path.cwd() / ".test-search-application" / uuid4().hex
    path.mkdir(parents=True)
    return path
