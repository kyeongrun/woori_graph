"""Local, dependency-free web surface for interactive graph questions."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from typing import Any, Protocol
from urllib.parse import urlsplit

from .artifacts import build_graph_payload
from .config import SearchPipelineConfig
from .models import SearchResult


LOGGER = logging.getLogger(__name__)
_MAX_REQUEST_BYTES = 64 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/app.css": ("app.css", "text/css; charset=utf-8"),
    "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


class SearchService(Protocol):
    def search(self, query: str) -> SearchResult: ...


class SearchWebServer(HTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        application: SearchService,
    ) -> None:
        self.search_application = application
        super().__init__(server_address, SearchRequestHandler)


class SearchRequestHandler(BaseHTTPRequestHandler):
    server: SearchWebServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._send_json({"status": "ok"})
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        asset = _ASSETS.get(path)
        if asset is None:
            self._send_json({"error": "요청한 경로를 찾을 수 없습니다."}, status=404)
            return
        name, content_type = asset
        payload = _read_web_asset(name)
        self.send_response(HTTPStatus.OK)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if urlsplit(self.path).path != "/api/search":
            self._send_json({"error": "요청한 경로를 찾을 수 없습니다."}, status=404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"error": "올바르지 않은 요청 크기입니다."}, status=400)
            return
        if content_length < 1 or content_length > _MAX_REQUEST_BYTES:
            self._send_json({"error": "질문 요청의 크기가 허용 범위를 벗어났습니다."}, status=413)
            return
        try:
            request = json.loads(self.rfile.read(content_length).decode("utf-8"))
            query = str(request.get("query", "")).strip()
            if not 2 <= len(query) <= 500:
                raise ValueError("질문은 2자 이상 500자 이하로 입력해주세요.")
            result = self.server.search_application.search(query)
            config = getattr(self.server.search_application, "config", None)
            self._send_json(build_search_web_payload(result, config=config))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "JSON 요청 형식을 확인해주세요."}, status=400)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception:
            LOGGER.exception("interactive graph search failed")
            self._send_json(
                {"error": "검색 처리 중 오류가 발생했습니다. 서버 로그를 확인해주세요."},
                status=500,
            )

    def log_message(self, message_format: str, *args: object) -> None:
        LOGGER.info("search-web %s - %s", self.address_string(), message_format % args)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )


def create_search_web_server(
    application: SearchService,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> SearchWebServer:
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("search-web host must be loopback (127.0.0.1, localhost, or ::1)")
    if not 0 <= port <= 65535:
        raise ValueError("search-web port must be between 0 and 65535")
    return SearchWebServer((host, port), application)


def build_search_web_payload(
    result: SearchResult,
    *,
    config: SearchPipelineConfig | None = None,
) -> dict[str, Any]:
    graph = build_graph_payload(result, path_limit=20)
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for edge in graph["edges"]:
        for evidence in edge["evidence"]:
            evidence_by_id.setdefault(
                evidence["relation_mention_id"],
                {
                    **evidence,
                    "relation_type_name": edge["relation"],
                    "source_name": edge["source_name"],
                    "target_name": edge["target_name"],
                },
            )
    evidence = sorted(
        evidence_by_id.values(),
        key=lambda item: (
            item["document_title"],
            json.dumps(item.get("source_ref", {}), ensure_ascii=False, sort_keys=True),
            item["relation_mention_id"],
        ),
    )
    entity_top_k = config.entity_top_k if config else result.stats.entity_candidates
    relation_top_k = config.relation_top_k if config else result.stats.relation_candidates
    neighbor_limit = config.max_neighbors_per_entity if config else None
    beam_width = config.path_beam_width if config else None
    max_paths = config.max_paths if config else result.stats.paths_returned
    return {
        "query": result.query,
        "answer": result.answer,
        "graph": graph,
        "evidence": evidence,
        "stats": result.stats.to_dict(),
        "search_plan": {
            "candidate_retrieval": {
                "method": "OpenSearch 키워드(BM25) + 벡터 k-NN 결과를 RRF로 결합",
                "entity_top_k": entity_top_k,
                "entity_selected": len(result.entity_candidates),
                "relation_top_k": relation_top_k,
                "relation_selected": len(result.relation_candidates),
                "per_mode_fetch_multiplier": 2,
            },
            "traversal": {
                "max_hops": config.max_hops if config else result.stats.max_hops_requested,
                "relation_candidate_only": False,
                "rule": (
                    "각 hop에서 frontier 엔티티의 인접 관계를 조회합니다. 관계 Top-K에 "
                    "포함되지 않은 관계도 확장 대상이며, 질문·관계 후보 관련도·엔티티명 "
                    "일치·근거 수로 점수화한 뒤 엔티티별 neighbor 한도와 hop별 beam으로 줄입니다."
                ),
                "max_neighbors_per_entity": neighbor_limit,
                "path_beam_width": beam_width,
                "hops": [
                    {
                        "hop": item.hop,
                        "frontier_entities": item.frontier_entities,
                        "fetched_edges": item.fetched_edges,
                        "query_edge_limit": item.query_edge_limit,
                        "neighbor_limit_per_entity": item.neighbor_limit_per_entity,
                        "generated_paths": item.generated_paths,
                        "beam_width": item.beam_width,
                        "retained_paths": item.retained_paths,
                    }
                    for item in result.stats.hop_diagnostics
                ],
            },
            "final_selection": {
                "deduplicated_paths": result.stats.deduplicated_paths,
                "evidence_path_pool": result.stats.evidence_path_pool,
                "max_paths": max_paths,
                "returned_paths": result.stats.paths_returned,
                "evidence_per_relation": (
                    config.evidence_per_relation if config else None
                ),
                "timeout_seconds": config.timeout_seconds if config else None,
                "adaptations": list(result.stats.adaptations),
            },
        },
        "selected_candidates": {
            "entities": [
                {"rank": rank, **candidate.to_dict()}
                for rank, candidate in enumerate(result.entity_candidates, start=1)
            ],
            "relations": [
                {"rank": rank, **candidate.to_dict()}
                for rank, candidate in enumerate(result.relation_candidates, start=1)
            ],
        },
        "counts": {
            "paths": len(result.paths),
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "evidence": len(evidence),
            "documents": len({item["document_id"] for item in evidence}),
        },
    }


def _read_web_asset(name: str) -> bytes:
    return (
        resources.files("woori_graph.search")
        .joinpath("web_assets", name)
        .read_bytes()
    )
