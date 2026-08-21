from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from woori_graph.search.config import SearchPipelineConfig
from woori_graph.search.models import (
    EntityCandidate,
    Evidence,
    GraphEdge,
    GraphPath,
    HopDiagnostic,
    RelationCandidate,
    SearchResult,
    SearchStats,
)
from woori_graph.search.web import (
    build_search_web_payload,
    create_search_web_server,
)


def _config() -> SearchPipelineConfig:
    from pathlib import Path

    return SearchPipelineConfig(
        artifact_root=Path("artifacts/search"),
        env_file=None,
        opensearch_url="http://127.0.0.1:19200",
        entity_index="entities",
        relation_index="relations",
        postgres_dsn_env="GRAPH_POSTGRES_DSN",
        max_hops=3,
        entity_top_k=8,
        relation_top_k=20,
        max_neighbors_per_entity=50,
        path_beam_width=160,
        max_paths=50,
        evidence_per_relation=3,
        timeout_seconds=180,
        request_timeout_seconds=30,
    )


def _result(query: str = "금융위원회와 금융기관") -> SearchResult:
    evidence = Evidence(
        relation_mention_id="mention-1",
        relation_id="relation-1",
        semantic_unit_id="unit-1",
        document_id="document-1",
        document_title="금융위원회의 설치 등에 관한 법률",
        source_path="법률/법률.md",
        source_ref={"article": "제7조", "paragraph": 1, "item_path": []},
        raw_subject="금융위원회",
        raw_predicate="통지한다",
        raw_object="금융기관",
    )
    edge = GraphEdge(
        relation_id="relation-1",
        source_entity_id="entity-1",
        source_name="금융위원회",
        relation_type_id="relation-type-1",
        relation_type_name="통지하다",
        polarity="POSITIVE",
        target_entity_id="entity-2",
        target_name="금융기관",
        evidence_count=1,
        evidences=[evidence],
    )
    path = GraphPath(
        start_entity_id="entity-1",
        start_name="금융위원회",
        end_entity_id="entity-2",
        end_name="금융기관",
        edges=[edge],
        traversed_entity_ids=["entity-1", "entity-2"],
        traversed_entity_names=["금융위원회", "금융기관"],
        score=1.2,
        document_ids=["document-1"],
        document_titles=["금융위원회의 설치 등에 관한 법률"],
    )
    return SearchResult(
        query=query,
        answer="금융위원회는 금융기관에 통지합니다.",
        entity_candidates=[
            EntityCandidate(
                "entity-1", "금융위원회", "ORGANIZATION", 0.032, ("keyword", "vector")
            )
        ],
        relation_candidates=[
            RelationCandidate(
                "relation-1",
                "entity-1",
                "금융위원회",
                "relation-type-1",
                "통지하다",
                "entity-2",
                "금융기관",
                0.031,
                ("keyword", "vector"),
            )
        ],
        paths=[path],
        stats=SearchStats(
            duration_seconds=0.8,
            timed_out=False,
            max_hops_requested=3,
            max_hops_reached=1,
            entity_candidates=1,
            relation_candidates=1,
            paths_considered=4,
            paths_returned=1,
            hop_diagnostics=(
                HopDiagnostic(1, 2, 4, 200, 50, 4, 160, 4),
            ),
            deduplicated_paths=3,
            evidence_path_pool=3,
        ),
    )


class FakeSearchApplication:
    config = _config()

    def search(self, query: str) -> SearchResult:
        return _result(query)


def test_web_payload_exposes_search_plan_candidates_and_hop_diagnostics():
    payload = build_search_web_payload(_result(), config=_config())

    assert payload["search_plan"]["candidate_retrieval"]["entity_top_k"] == 8
    assert payload["search_plan"]["candidate_retrieval"]["relation_top_k"] == 20
    assert payload["search_plan"]["traversal"]["relation_candidate_only"] is False
    assert payload["search_plan"]["traversal"]["hops"][0]["fetched_edges"] == 4
    assert payload["search_plan"]["final_selection"]["max_paths"] == 50
    assert payload["selected_candidates"]["entities"][0]["canonical_name"] == "금융위원회"
    assert payload["selected_candidates"]["relations"][0]["relation_type_name"] == "통지하다"
    assert payload["evidence"][0]["document_title"].startswith("금융위원회")


def test_web_server_serves_ui_health_and_interactive_search():
    server = create_search_web_server(FakeSearchApplication(), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(f"{base_url}/", timeout=3) as response:
            html = response.read().decode("utf-8")
            assert response.status == 200
            assert "법령 관계 탐색" in html
            assert "탐색 상세" in html
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]

        with urlopen(f"{base_url}/api/health", timeout=3) as response:
            assert json.loads(response.read())["status"] == "ok"

        request = Request(
            f"{base_url}/api/search",
            data=json.dumps({"query": "금융위원회와 금융기관"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read())
            assert payload["query"] == "금융위원회와 금융기관"
            assert payload["counts"]["nodes"] == 2
            assert payload["search_plan"]["traversal"]["hops"]

        bad_request = Request(
            f"{base_url}/api/search",
            data=b'{"query":""}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urlopen(bad_request, timeout=3)
        except HTTPError as error:
            assert error.code == 400
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("empty query should return HTTP 400")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_web_server_rejects_non_loopback_binding():
    try:
        create_search_web_server(FakeSearchApplication(), host="0.0.0.0", port=0)
    except ValueError as error:
        assert "loopback" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("non-loopback search-web binding should be rejected")
