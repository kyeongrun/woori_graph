"""Repository ports and concrete OpenSearch/PostgreSQL search adapters."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import httpx

from .models import EntityCandidate, Evidence, GraphEdge, RelationCandidate


class CandidateRepository(Protocol):
    def hybrid_candidates(
        self,
        query: str,
        query_vector: Sequence[float],
        *,
        entity_top_k: int,
        relation_top_k: int,
    ) -> tuple[list[EntityCandidate], list[RelationCandidate]]: ...


class GraphRepository(Protocol):
    def expand(
        self,
        entity_ids: Sequence[str],
        *,
        max_edges: int,
        per_entity: int,
        preferred_relation_ids: Sequence[str] = (),
    ) -> list[GraphEdge]: ...

    def attach_evidence(
        self,
        edges: Sequence[GraphEdge],
        *,
        per_relation: int,
    ) -> None: ...


class OpenSearchCandidateRepository:
    """Hybrid BM25/vector retrieval with reciprocal-rank fusion."""

    def __init__(
        self,
        base_url: str,
        *,
        entity_index: str = "entities",
        relation_index: str = "relations",
        timeout_seconds: float = 30,
        credentials: tuple[str, str] | None = None,
        verify_tls: bool = True,
        rrf_rank_constant: int = 60,
    ):
        self._base_url = base_url.rstrip("/")
        self._entity_index = entity_index
        self._relation_index = relation_index
        self._rrf_rank_constant = rrf_rank_constant
        self._client = httpx.Client(
            timeout=timeout_seconds,
            auth=credentials,
            verify=verify_tls,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OpenSearchCandidateRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def hybrid_candidates(
        self,
        query: str,
        query_vector: Sequence[float],
        *,
        entity_top_k: int,
        relation_top_k: int,
    ) -> tuple[list[EntityCandidate], list[RelationCandidate]]:
        entity_size = max(entity_top_k * 2, entity_top_k)
        relation_size = max(relation_top_k * 2, relation_top_k)
        searches = [
            (
                self._entity_index,
                _keyword_query(
                    query,
                    fields=("canonical_name^4", "aliases^2"),
                    exact_field="canonical_name.keyword",
                    keyword_fields=("canonical_name.keyword", "aliases.keyword"),
                    size=entity_size,
                ),
            ),
            (
                self._entity_index,
                _vector_query(query_vector, size=entity_size),
            ),
            (
                self._relation_index,
                _keyword_query(
                    query,
                    fields=("source_name^3", "target_name^3", "relation_type_name^2"),
                    exact_field=None,
                    keyword_fields=(
                        "source_name.keyword",
                        "target_name.keyword",
                        "relation_type_name",
                    ),
                    size=relation_size,
                ),
            ),
            (
                self._relation_index,
                _vector_query(query_vector, size=relation_size),
            ),
        ]
        payload = "".join(
            json.dumps({"index": index}, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            + json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            for index, body in searches
        )
        response = self._client.post(
            f"{self._base_url}/_msearch",
            content=payload.encode("utf-8"),
            headers={"Content-Type": "application/x-ndjson"},
        )
        response.raise_for_status()
        responses = response.json().get("responses")
        if not isinstance(responses, list) or len(responses) != 4:
            raise RuntimeError("OpenSearch _msearch returned an unexpected response count")
        for item in responses:
            if isinstance(item, Mapping) and item.get("error"):
                raise RuntimeError(f"OpenSearch search failed: {item['error']!r}")

        entity_hits = [
            _hits(responses[0]),
            _hits(responses[1]),
        ]
        relation_hits = [
            _hits(responses[2]),
            _hits(responses[3]),
        ]
        entities = self._fuse_entities(entity_hits)[:entity_top_k]
        relations = self._fuse_relations(relation_hits)[:relation_top_k]
        return entities, relations

    def _fuse_entities(
        self, ranked_hits: Sequence[Sequence[dict[str, Any]]]
    ) -> list[EntityCandidate]:
        buckets: dict[str, dict[str, Any]] = {}
        labels = ("keyword", "vector")
        for label, hits in zip(labels, ranked_hits, strict=True):
            for rank, hit in enumerate(hits, start=1):
                source = hit["_source"]
                identifier = str(source["entity_id"])
                bucket = buckets.setdefault(
                    identifier,
                    {"source": source, "score": 0.0, "matched_by": []},
                )
                bucket["score"] += 1.0 / (self._rrf_rank_constant + rank)
                bucket["matched_by"].append(label)
        return sorted(
            (
                EntityCandidate(
                    entity_id=identifier,
                    canonical_name=str(bucket["source"]["canonical_name"]),
                    entity_type=str(bucket["source"].get("entity_type", "OTHER")),
                    score=float(bucket["score"]),
                    matched_by=tuple(bucket["matched_by"]),
                )
                for identifier, bucket in buckets.items()
            ),
            key=lambda item: (-item.score, item.canonical_name, item.entity_id),
        )

    def _fuse_relations(
        self, ranked_hits: Sequence[Sequence[dict[str, Any]]]
    ) -> list[RelationCandidate]:
        buckets: dict[str, dict[str, Any]] = {}
        labels = ("keyword", "vector")
        for label, hits in zip(labels, ranked_hits, strict=True):
            for rank, hit in enumerate(hits, start=1):
                source = hit["_source"]
                identifier = str(source["relation_id"])
                bucket = buckets.setdefault(
                    identifier,
                    {"source": source, "score": 0.0, "matched_by": []},
                )
                bucket["score"] += 1.0 / (self._rrf_rank_constant + rank)
                bucket["matched_by"].append(label)
        return sorted(
            (
                RelationCandidate(
                    relation_id=identifier,
                    source_entity_id=str(bucket["source"]["source_entity_id"]),
                    source_name=str(bucket["source"]["source_name"]),
                    relation_type_id=str(bucket["source"]["relation_type_id"]),
                    relation_type_name=str(bucket["source"]["relation_type_name"]),
                    target_entity_id=str(bucket["source"]["target_entity_id"]),
                    target_name=str(bucket["source"]["target_name"]),
                    score=float(bucket["score"]),
                    matched_by=tuple(bucket["matched_by"]),
                )
                for identifier, bucket in buckets.items()
            ),
            key=lambda item: (-item.score, item.relation_id),
        )


class PostgresGraphRepository:
    """Indexed relation expansion plus document/evidence retrieval from RDB."""

    def __init__(self, dsn: str, *, statement_timeout_seconds: float = 30):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "PostgreSQL search requires the optional 'search' dependencies"
            ) from exc
        self._psycopg = psycopg
        self._connection = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        milliseconds = max(1, int(statement_timeout_seconds * 1000))
        with self._connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {milliseconds}")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "PostgresGraphRepository":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def expand(
        self,
        entity_ids: Sequence[str],
        *,
        max_edges: int,
        per_entity: int,
        preferred_relation_ids: Sequence[str] = (),
    ) -> list[GraphEdge]:
        identifiers = sorted(set(entity_ids))
        if not identifiers:
            return []
        preferred = sorted(set(preferred_relation_ids))
        sql = """
            WITH frontier(entity_id) AS (
                SELECT unnest(%s::uuid[])
            ), incident AS (
                SELECT f.entity_id AS frontier_entity_id, r.*
                FROM frontier AS f
                JOIN graph.relation AS r ON r.source_entity_id = f.entity_id
                UNION ALL
                SELECT f.entity_id AS frontier_entity_id, r.*
                FROM frontier AS f
                JOIN graph.relation AS r ON r.target_entity_id = f.entity_id
                WHERE r.source_entity_id <> f.entity_id
            ), ranked AS (
                SELECT incident.*,
                       relation_id = ANY(%s::uuid[]) AS preferred,
                       row_number() OVER (
                           PARTITION BY frontier_entity_id
                           ORDER BY relation_id = ANY(%s::uuid[]) DESC,
                                    evidence_count DESC, relation_id
                       ) AS neighbor_rank
                FROM incident
            ), bounded AS (
                SELECT DISTINCT ON (relation_id) *
                FROM ranked
                WHERE neighbor_rank <= %s
                ORDER BY relation_id, preferred DESC, neighbor_rank
            )
            SELECT r.relation_id::text, r.source_entity_id::text,
                   r.source_name, r.relation_type_id::text,
                   rt.canonical_name AS relation_type_name, rt.polarity,
                   r.target_entity_id::text, r.target_name, r.evidence_count
            FROM bounded AS r
            JOIN graph.relation_type AS rt USING (relation_type_id)
            ORDER BY r.preferred DESC, r.evidence_count DESC, r.relation_id
            LIMIT %s
        """
        with self._connection.cursor() as cursor:
            cursor.execute(
                sql,
                (identifiers, preferred, preferred, per_entity, max_edges),
            )
            return [_edge_from_row(row) for row in cursor.fetchall()]

    def attach_evidence(
        self,
        edges: Sequence[GraphEdge],
        *,
        per_relation: int,
    ) -> None:
        edge_by_id = {edge.relation_id: edge for edge in edges}
        if not edge_by_id:
            return
        sql = """
            WITH per_document AS (
                SELECT re.relation_mention_id::text, re.relation_id::text,
                       su.semantic_unit_id::text, su.document_id::text,
                       d.document_title, d.source_path, su.source_ref,
                       su.context_text, su.unit_text, su.unit_kind,
                       re.raw_subject, re.raw_predicate, re.raw_object,
                       row_number() OVER (
                           PARTITION BY re.relation_id, su.document_id
                           ORDER BY su.source_ref::text, re.relation_mention_id
                       ) AS document_rank
                FROM graph.relation_evidence AS re
                JOIN graph.semantic_unit AS su USING (semantic_unit_id)
                JOIN graph.document AS d ON d.document_id = su.document_id
                WHERE re.relation_id = ANY(%s::uuid[])
            ), ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY relation_id
                    ORDER BY document_title, source_ref::text,
                             relation_mention_id
                ) AS evidence_rank
                FROM per_document
                WHERE document_rank = 1
            )
            SELECT * FROM ranked
            WHERE evidence_rank <= %s
            ORDER BY relation_id, evidence_rank
        """
        with self._connection.cursor() as cursor:
            cursor.execute(sql, (sorted(edge_by_id), per_relation))
            for row in cursor.fetchall():
                edge_by_id[row["relation_id"]].evidences.append(_evidence_from_row(row))

    def load_all_edges_with_evidence(self) -> list[GraphEdge]:
        """Load the current snapshot for offline cross-document QA discovery."""

        relation_sql = """
            SELECT r.relation_id::text, r.source_entity_id::text,
                   r.source_name, r.relation_type_id::text,
                   rt.canonical_name AS relation_type_name, rt.polarity,
                   r.target_entity_id::text, r.target_name, r.evidence_count
            FROM graph.relation AS r
            JOIN graph.relation_type AS rt USING (relation_type_id)
            ORDER BY r.relation_id
        """
        evidence_sql = """
            SELECT re.relation_mention_id::text, re.relation_id::text,
                   su.semantic_unit_id::text, su.document_id::text,
                   d.document_title, d.source_path, su.source_ref,
                   su.context_text, su.unit_text, su.unit_kind,
                   re.raw_subject, re.raw_predicate, re.raw_object
            FROM graph.relation_evidence AS re
            JOIN graph.semantic_unit AS su USING (semantic_unit_id)
            JOIN graph.document AS d ON d.document_id = su.document_id
            ORDER BY re.relation_id, d.document_title, su.source_ref::text,
                     re.relation_mention_id
        """
        with self._connection.cursor() as cursor:
            cursor.execute(relation_sql)
            edges = [_edge_from_row(row) for row in cursor.fetchall()]
            edge_by_id = {edge.relation_id: edge for edge in edges}
            cursor.execute(evidence_sql)
            for row in cursor.fetchall():
                edge = edge_by_id.get(row["relation_id"])
                if edge is not None:
                    edge.evidences.append(_evidence_from_row(row))
        return edges


def _keyword_query(
    query: str,
    *,
    fields: Sequence[str],
    exact_field: str | None,
    keyword_fields: Sequence[str],
    size: int,
) -> dict[str, Any]:
    should: list[dict[str, Any]] = [
        {
            "multi_match": {
                "query": query,
                "fields": list(fields),
                "type": "best_fields",
                "operator": "or",
            }
        }
    ]
    if exact_field:
        should.append({"term": {exact_field: {"value": query, "boost": 8}}})
    for term in _query_terms(query):
        for keyword_field in keyword_fields:
            should.append(
                {"term": {keyword_field: {"value": term, "boost": 10}}}
            )
            should.append(
                {
                    "wildcard": {
                        keyword_field: {
                            "value": f"*{term}*",
                            "boost": 2,
                        }
                    }
                }
            )
    return {
        "size": size,
        "track_total_hits": False,
        "_source": {"excludes": ["embedding"]},
        "query": {"bool": {"should": should, "minimum_should_match": 1}},
    }


_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_PARTICLE_SUFFIXES = (
    "으로부터",
    "에서부터",
    "에게서는",
    "에서는",
    "에게서",
    "으로는",
    "로부터",
    "까지는",
    "과의",
    "와의",
    "에게",
    "에서",
    "으로",
    "부터",
    "까지",
    "처럼",
    "보다",
    "에는",
    "로는",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "와",
    "과",
    "의",
    "에",
    "로",
)
_QUERY_STOP_WORDS = {
    "어떻게",
    "연결",
    "연결되는가",
    "무엇인가",
    "무엇",
    "근거",
    "법령",
    "각",
}


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for match in _WORD_RE.finditer(query):
        value = match.group(0)
        for suffix in _PARTICLE_SUFFIXES:
            if value.endswith(suffix) and len(value) - len(suffix) >= 2:
                value = value[: -len(suffix)]
                break
        if len(value) >= 2 and value not in _QUERY_STOP_WORDS and value not in terms:
            terms.append(value)
    return terms[:8]


def _vector_query(vector: Sequence[float], *, size: int) -> dict[str, Any]:
    return {
        "size": size,
        "track_total_hits": False,
        "_source": {"excludes": ["embedding"]},
        "query": {"knn": {"embedding": {"vector": list(vector), "k": size}}},
    }


def _hits(response: Mapping[str, Any]) -> list[dict[str, Any]]:
    hits = response.get("hits", {})
    if not isinstance(hits, Mapping) or not isinstance(hits.get("hits"), list):
        raise RuntimeError("OpenSearch search response does not contain hits.hits")
    return list(hits["hits"])


def _edge_from_row(row: Mapping[str, Any]) -> GraphEdge:
    return GraphEdge(
        relation_id=str(row["relation_id"]),
        source_entity_id=str(row["source_entity_id"]),
        source_name=str(row["source_name"]),
        relation_type_id=str(row["relation_type_id"]),
        relation_type_name=str(row["relation_type_name"]),
        polarity=str(row["polarity"]) if row.get("polarity") is not None else None,
        target_entity_id=str(row["target_entity_id"]),
        target_name=str(row["target_name"]),
        evidence_count=int(row["evidence_count"]),
    )


def _evidence_from_row(row: Mapping[str, Any]) -> Evidence:
    source_ref = row["source_ref"]
    if isinstance(source_ref, str):
        source_ref = json.loads(source_ref)
    return Evidence(
        relation_mention_id=str(row["relation_mention_id"]),
        relation_id=str(row["relation_id"]),
        semantic_unit_id=str(row["semantic_unit_id"]),
        document_id=str(row["document_id"]),
        document_title=str(row["document_title"]),
        source_path=str(row["source_path"]),
        source_ref=dict(source_ref),
        raw_subject=str(row["raw_subject"]),
        raw_predicate=str(row["raw_predicate"]),
        raw_object=str(row["raw_object"]),
        context_text=str(row.get("context_text", "")),
        unit_text=str(row.get("unit_text", "")),
        unit_kind=str(row.get("unit_kind", "unknown")),
    )
