from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from woori_graph.search.artifacts import write_search_artifacts
from woori_graph.search.config import SearchPipelineConfig, load_search_config
from woori_graph.search.cross_document import discover_cross_document_questions
from woori_graph.search.models import (
    EntityCandidate,
    Evidence,
    GraphEdge,
    RelationCandidate,
)
from woori_graph.search.pipeline import GraphSearchPipeline


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeEmbeddingClient:
    model = "fake-search-embedding"
    dimension = 3

    def __init__(self, clock: FakeClock | None = None, advance: float = 0) -> None:
        self.clock = clock
        self.advance = advance

    def embed_queries(self, texts):
        if self.clock is not None:
            self.clock.value += self.advance
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_documents(self, texts):
        return self.embed_queries(texts)


class FakeCandidates:
    def __init__(self) -> None:
        self.requested = None

    def hybrid_candidates(
        self, query, query_vector, *, entity_top_k, relation_top_k
    ):
        self.requested = (entity_top_k, relation_top_k)
        return (
            [EntityCandidate("a", "기관A", "ORGANIZATION", 1.0, ("keyword",))],
            [
                RelationCandidate(
                    "r1", "a", "기관A", "t1", "감독하다", "b", "업무B", 1.0,
                    ("keyword", "vector"),
                )
            ],
        )


class FakeGraph:
    def __init__(self) -> None:
        self.edges = [
            _edge("r1", "a", "기관A", "감독하다", "b", "업무B", "법률A"),
            _edge("r2", "b", "업무B", "연결하다", "c", "기관C", "법률B"),
            _edge("r3", "c", "기관C", "보고하다", "d", "위원회D", "법률C"),
        ]

    def expand(self, entity_ids, *, max_edges):
        identifiers = set(entity_ids)
        return [
            edge
            for edge in self.edges
            if edge.source_entity_id in identifiers or edge.target_entity_id in identifiers
        ][:max_edges]

    def attach_evidence(self, edges, *, per_relation):
        by_id = {edge.relation_id: edge for edge in self.edges}
        for edge in edges:
            edge.evidences[:] = by_id[edge.relation_id].evidences[:per_relation]


def _config(tmp_path: Path, **overrides) -> SearchPipelineConfig:
    values = {
        "artifact_root": tmp_path / "artifacts",
        "env_file": None,
        "opensearch_url": "http://localhost:9200",
        "entity_index": "entities",
        "relation_index": "relations",
        "postgres_dsn_env": "TEST_POSTGRES_DSN",
        "max_hops": 3,
        "entity_top_k": 8,
        "relation_top_k": 20,
        "max_neighbors_per_entity": 10,
        "path_beam_width": 20,
        "max_paths": 20,
        "evidence_per_relation": 2,
        "timeout_seconds": 180,
        "request_timeout_seconds": 30,
    }
    values.update(overrides)
    return SearchPipelineConfig(**values)


def _workspace_tmp() -> Path:
    path = Path.cwd() / ".test-search-artifacts" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def _edge(
    relation_id: str,
    source_id: str,
    source_name: str,
    relation_name: str,
    target_id: str,
    target_name: str,
    document_title: str,
) -> GraphEdge:
    edge = GraphEdge(
        relation_id=relation_id,
        source_entity_id=source_id,
        source_name=source_name,
        relation_type_id="type-" + relation_id,
        relation_type_name=relation_name,
        polarity="POSITIVE",
        target_entity_id=target_id,
        target_name=target_name,
        evidence_count=1,
    )
    edge.evidences.append(
        Evidence(
            relation_mention_id="mention-" + relation_id,
            relation_id=relation_id,
            semantic_unit_id="unit-" + relation_id,
            document_id="document-" + document_title,
            document_title=document_title,
            source_path=document_title + ".md",
            source_ref={"article": "제1조", "paragraph": 1, "item_path": []},
            raw_subject=source_name,
            raw_predicate=relation_name,
            raw_object=target_name,
        )
    )
    return edge


def test_search_pipeline_reaches_three_hops_and_keeps_document_evidence():
    tmp_path = _workspace_tmp()
    pipeline = GraphSearchPipeline(
        FakeCandidates(), FakeGraph(), FakeEmbeddingClient(), _config(tmp_path)
    )

    result = pipeline.search("기관A와 위원회D는 어떻게 연결되는가?")

    assert result.stats.max_hops_reached == 3
    three_hop = next(path for path in result.paths if path.hops == 3)
    assert three_hop.traversed_entity_ids == ["a", "b", "c", "d"]
    assert three_hop.document_titles == ["법률A", "법률B", "법률C"]
    assert "법률A 제1조 제1항" in result.answer


def test_search_pipeline_reduces_top_k_after_soft_time_threshold():
    tmp_path = _workspace_tmp()
    clock = FakeClock()
    candidates = FakeCandidates()
    pipeline = GraphSearchPipeline(
        candidates,
        FakeGraph(),
        FakeEmbeddingClient(clock, advance=125),
        _config(tmp_path),
        clock=clock,
    )

    result = pipeline.search("기관A의 연결")

    assert candidates.requested == (4, 10)
    assert any(note.startswith("entity_top_k:8->4") for note in result.stats.adaptations)


def test_search_pipeline_returns_timeout_before_candidate_call():
    tmp_path = _workspace_tmp()
    clock = FakeClock()
    candidates = FakeCandidates()
    pipeline = GraphSearchPipeline(
        candidates,
        FakeGraph(),
        FakeEmbeddingClient(clock, advance=181),
        _config(tmp_path),
        clock=clock,
    )

    result = pipeline.search("시간 초과")

    assert result.stats.timed_out is True
    assert candidates.requested is None


def test_search_config_resolves_relative_paths_and_caps_timeout():
    tmp_path = _workspace_tmp()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("EMBEDDING_MODEL=fake\n", encoding="utf-8")
    config_path = config_dir / "search.toml"
    config_path.write_text(
        """
[paths]
artifact_root = "../output"
env_file = "../.env"
[services]
opensearch_url = "http://localhost:9200"
postgres_dsn_env = "TEST_POSTGRES_DSN"
[search]
max_hops = 3
timeout_seconds = 180
request_timeout_seconds = 30
""".strip(),
        encoding="utf-8",
    )

    config = load_search_config(config_path)

    assert config.artifact_root == (tmp_path / "output").resolve()
    assert config.env_file == env_file.resolve()
    with pytest.raises(ValueError, match="between 1 and 180"):
        _config(tmp_path, timeout_seconds=181).validate()


def test_cross_document_question_discovery_requires_different_documents():
    edges = [
        _edge("r1", "a", "금융기관", "보고하다", "b", "금융위원회", "법률A"),
        _edge("r2", "b", "금융위원회", "감독하다", "c", "은행", "법률B"),
    ]

    questions = discover_cross_document_questions(edges, count=1)

    assert len(questions) == 1
    assert questions[0].expected_document_titles == ("법률A", "법률B")
    assert "금융위원회" in questions[0].question


def test_search_artifacts_include_answer_graph_and_manifest():
    tmp_path = _workspace_tmp()
    config = _config(tmp_path)
    config_path = tmp_path / "search.toml"
    config_path.write_text("[test]\n", encoding="utf-8")
    result = GraphSearchPipeline(
        FakeCandidates(), FakeGraph(), FakeEmbeddingClient(), config
    ).search("기관A와 위원회D")

    manifest = write_search_artifacts(
        result,
        config=config,
        config_path=config_path,
        embedding_model="fake-search-embedding",
        embedding_dimension=3,
        run_id="test-search",
    )

    final = config.artifact_root / "test-search" / "final"
    assert manifest["passed"] is True
    assert manifest["config"]["path"] == "search.toml"
    assert not Path(manifest["config"]["path"]).is_absolute()
    assert (final / "answer.md").is_file()
    assert (final / "graph.html").is_file()
    graph = json.loads((final / "graph.json").read_text(encoding="utf-8"))
    assert graph["nodes"]
    assert graph["edges"]
    html = (final / "graph.html").read_text(encoding="utf-8")
    assert "<!doctype html>" in html
    assert "graph-data" in html
