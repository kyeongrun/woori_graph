"""Bounded hybrid retrieval and three-hop graph exploration."""

from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..embeddings import EmbeddingClient
from .answering import GroundedAnswerGenerator
from .config import SearchPipelineConfig
from .models import (
    AnswerMetadata,
    EntityCandidate,
    GraphEdge,
    GraphPath,
    HopDiagnostic,
    RelationCandidate,
    SearchResult,
    SearchStats,
)
from .repositories import CandidateRepository, GraphRepository


@dataclass(frozen=True)
class _PathState:
    start_entity_id: str
    start_name: str
    current_entity_id: str
    current_name: str
    entity_ids: tuple[str, ...]
    entity_names: tuple[str, ...]
    edges: tuple[GraphEdge, ...]
    score: float


class GraphSearchPipeline:
    """Query embedding -> hybrid candidates -> bounded graph paths -> answer."""

    def __init__(
        self,
        candidate_repository: CandidateRepository,
        graph_repository: GraphRepository,
        embedding_client: EmbeddingClient,
        config: SearchPipelineConfig,
        *,
        answer_generator: GroundedAnswerGenerator | None = None,
        answer_unavailable_reason: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        config.validate()
        self._candidates = candidate_repository
        self._graph = graph_repository
        self._embeddings = embedding_client
        self._config = config
        self._answer_generator = answer_generator
        self._answer_unavailable_reason = answer_unavailable_reason
        self._clock = clock

    def search(self, query: str) -> SearchResult:
        query = query.strip()
        if not query:
            raise ValueError("query must be non-empty")
        started = self._clock()
        deadline = started + self._config.timeout_seconds
        adaptations: list[str] = []

        query_vectors = self._embeddings.embed_queries([query])
        if len(query_vectors) != 1:
            raise RuntimeError("embedding client must return exactly one query vector")
        if self._clock() >= deadline:
            return self._empty_timeout_result(query, started, adaptations)

        entity_top_k = self._adaptive_limit(
            self._config.entity_top_k,
            started,
            deadline,
            "entity_top_k",
            adaptations,
        )
        relation_top_k = self._adaptive_limit(
            self._config.relation_top_k,
            started,
            deadline,
            "relation_top_k",
            adaptations,
        )
        entities, relations = self._candidates.hybrid_candidates(
            query,
            query_vectors[0],
            entity_top_k=entity_top_k,
            relation_top_k=relation_top_k,
        )
        if self._clock() >= deadline:
            return self._candidate_timeout_result(
                query, entities, relations, started, adaptations
            )

        seeds = _merge_seed_candidates(entities, relations)
        relation_relevance = _normalized_relation_scores(relations)
        preferred_relation_ids = tuple(relation_relevance)
        states = [
            _PathState(
                start_entity_id=seed.entity_id,
                start_name=seed.canonical_name,
                current_entity_id=seed.entity_id,
                current_name=seed.canonical_name,
                entity_ids=(seed.entity_id,),
                entity_names=(seed.canonical_name,),
                edges=(),
                score=_normalized_entity_score(seed, seeds),
            )
            for seed in seeds
        ]
        all_paths: list[GraphPath] = []
        paths_considered = 0
        max_hops_reached = 0
        timed_out = False
        hop_diagnostics: list[HopDiagnostic] = []

        for hop in range(1, self._config.max_hops + 1):
            if self._clock() >= deadline:
                timed_out = True
                break
            neighbor_limit = self._adaptive_limit(
                self._config.max_neighbors_per_entity,
                started,
                deadline,
                f"hop_{hop}_neighbors",
                adaptations,
            )
            beam_width = self._adaptive_limit(
                self._config.path_beam_width,
                started,
                deadline,
                f"hop_{hop}_beam_width",
                adaptations,
            )
            frontier = sorted({state.current_entity_id for state in states})
            max_edges = max(1, min(5000, len(frontier) * neighbor_limit * 2))
            edges = self._graph.expand(
                frontier,
                max_edges=max_edges,
                per_entity=neighbor_limit,
                preferred_relation_ids=preferred_relation_ids,
            )
            adjacency: dict[str, list[GraphEdge]] = defaultdict(list)
            for edge in edges:
                if edge.source_entity_id in frontier:
                    adjacency[edge.source_entity_id].append(edge)
                if edge.target_entity_id in frontier:
                    adjacency[edge.target_entity_id].append(edge)

            next_states: list[_PathState] = []
            for state in states:
                incident = sorted(
                    adjacency.get(state.current_entity_id, ()),
                    key=lambda edge: (
                        -_edge_relevance(
                            edge,
                            query,
                            relation_relevance,
                            current_entity_id=state.current_entity_id,
                            branching_factor=len(
                                adjacency.get(state.current_entity_id, ())
                            ),
                        ),
                        edge.relation_id,
                    ),
                )[:neighbor_limit]
                for edge in incident:
                    other_id = edge.other_entity_id(state.current_entity_id)
                    if other_id in state.entity_ids:
                        continue
                    other_name = edge.entity_name(other_id)
                    edge_score = _edge_relevance(
                        edge,
                        query,
                        relation_relevance,
                        current_entity_id=state.current_entity_id,
                        branching_factor=len(
                            adjacency.get(state.current_entity_id, ())
                        ),
                    )
                    next_state = _PathState(
                        start_entity_id=state.start_entity_id,
                        start_name=state.start_name,
                        current_entity_id=other_id,
                        current_name=other_name,
                        entity_ids=(*state.entity_ids, other_id),
                        entity_names=(*state.entity_names, other_name),
                        edges=(*state.edges, edge),
                        score=state.score + edge_score / hop,
                    )
                    next_states.append(next_state)
                    all_paths.append(_state_to_path(next_state))
            paths_considered += len(next_states)
            max_hops_reached = hop if next_states else max_hops_reached
            retained_paths = min(len(next_states), beam_width)
            hop_diagnostics.append(
                HopDiagnostic(
                    hop=hop,
                    frontier_entities=len(frontier),
                    fetched_edges=len(edges),
                    query_edge_limit=max_edges,
                    neighbor_limit_per_entity=neighbor_limit,
                    generated_paths=len(next_states),
                    beam_width=beam_width,
                    retained_paths=retained_paths,
                )
            )
            if not next_states:
                break
            states = sorted(
                next_states,
                key=lambda item: (-item.score, item.entity_ids, _edge_ids(item.edges)),
            )[:beam_width]

        for path in all_paths:
            path.score += _query_path_coverage_boost(path, query)
        deduplicated = _deduplicate_paths(all_paths)
        provisional_limit = self._adaptive_limit(
            max(self._config.max_paths * 3, self._config.max_paths),
            started,
            deadline,
            "evidence_path_pool",
            adaptations,
        )
        provisional = _reserve_preferred_relation_paths(
            deduplicated,
            preferred_relation_ids=preferred_relation_ids,
            limit=provisional_limit,
        )
        unique_edges = _unique_edges(provisional)
        if unique_edges and self._clock() < deadline:
            evidence_limit = self._adaptive_limit(
                self._config.evidence_per_relation,
                started,
                deadline,
                "evidence_per_relation",
                adaptations,
            )
            self._graph.attach_evidence(unique_edges, per_relation=evidence_limit)
        elif self._clock() >= deadline:
            timed_out = True

        for path in provisional:
            _update_path_documents(path)
            path.score += _evidence_query_relevance(path, query)
        max_paths = self._adaptive_limit(
            self._config.max_paths,
            started,
            deadline,
            "max_paths",
            adaptations,
        )
        paths = sorted(
            provisional,
            key=lambda item: (
                -item.score,
                -len(item.document_ids),
                item.hops,
                tuple(edge.relation_id for edge in item.edges),
            ),
        )[:max_paths]
        duration = self._clock() - started
        timed_out = timed_out or duration >= self._config.timeout_seconds
        answer = build_grounded_answer(query, paths, timed_out=timed_out)
        answer_metadata = (
            AnswerMetadata(
                mode="llm",
                status="fallback",
                fallback_reason=self._answer_unavailable_reason,
            )
            if self._answer_unavailable_reason
            else AnswerMetadata(status="not_configured")
        )
        if self._answer_generator is not None and paths and not timed_out:
            answer, answer_metadata = self._answer_generator.generate(
                query,
                paths,
                fallback_answer=answer,
                deadline=deadline,
            )
        duration = self._clock() - started
        timed_out = timed_out or duration >= self._config.timeout_seconds
        stats = SearchStats(
            duration_seconds=round(duration, 6),
            timed_out=timed_out,
            max_hops_requested=self._config.max_hops,
            max_hops_reached=max_hops_reached,
            entity_candidates=len(entities),
            relation_candidates=len(relations),
            paths_considered=paths_considered,
            paths_returned=len(paths),
            adaptations=tuple(adaptations),
            hop_diagnostics=tuple(hop_diagnostics),
            deduplicated_paths=len(deduplicated),
            evidence_path_pool=len(provisional),
        )
        return SearchResult(
            query=query,
            answer=answer,
            entity_candidates=entities,
            relation_candidates=relations,
            paths=paths,
            stats=stats,
            answer_metadata=answer_metadata,
        )

    def _adaptive_limit(
        self,
        base: int,
        started: float,
        deadline: float,
        label: str,
        adaptations: list[str],
    ) -> int:
        total = deadline - started
        elapsed_ratio = (self._clock() - started) / total if total else 1.0
        factor = 1.0
        if elapsed_ratio >= 0.84:
            factor = 0.25
        elif elapsed_ratio >= 0.67:
            factor = 0.5
        value = max(1, int(base * factor))
        if value < base:
            note = f"{label}:{base}->{value}@{elapsed_ratio:.2f}"
            if note not in adaptations:
                adaptations.append(note)
        return value

    def _empty_timeout_result(
        self,
        query: str,
        started: float,
        adaptations: list[str],
    ) -> SearchResult:
        duration = self._clock() - started
        stats = SearchStats(
            duration_seconds=round(duration, 6),
            timed_out=True,
            max_hops_requested=self._config.max_hops,
            max_hops_reached=0,
            entity_candidates=0,
            relation_candidates=0,
            paths_considered=0,
            paths_returned=0,
            adaptations=tuple(adaptations),
        )
        return SearchResult(query, _timeout_answer(), [], [], [], stats)

    def _candidate_timeout_result(
        self,
        query: str,
        entities: list[EntityCandidate],
        relations: list[RelationCandidate],
        started: float,
        adaptations: list[str],
    ) -> SearchResult:
        duration = self._clock() - started
        stats = SearchStats(
            duration_seconds=round(duration, 6),
            timed_out=True,
            max_hops_requested=self._config.max_hops,
            max_hops_reached=0,
            entity_candidates=len(entities),
            relation_candidates=len(relations),
            paths_considered=0,
            paths_returned=0,
            adaptations=tuple(adaptations),
        )
        return SearchResult(query, _timeout_answer(), entities, relations, [], stats)


def build_grounded_answer(
    query: str,
    paths: Sequence[GraphPath],
    *,
    timed_out: bool,
    path_limit: int = 5,
) -> str:
    if not paths:
        return _timeout_answer() if timed_out else "검색된 근거 관계가 없습니다."
    lines = [f"질문: {query}", "", "그래프와 법령 근거에서 확인된 주요 연결은 다음과 같습니다."]
    for index, path in enumerate(paths[:path_limit], start=1):
        lines.extend(("", f"{index}. {_path_text(path)}"))
        evidence_lines: list[str] = []
        for edge in path.edges:
            for evidence in edge.evidences:
                citation = f"{evidence.document_title} {_source_ref_text(evidence.source_ref)}".strip()
                detail = (
                    f"{evidence.raw_subject} - {evidence.raw_predicate} → "
                    f"{evidence.raw_object}"
                )
                evidence_lines.append(f"   - 근거: {citation}; 원표현: {detail}")
        lines.extend(_deduplicate_text(evidence_lines))
    if timed_out:
        lines.extend(
            ("", "탐색 시간 한도에 도달하여 top-k와 경로 수를 축소한 부분 결과입니다.")
        )
    return "\n".join(lines)


def _merge_seed_candidates(
    entities: Sequence[EntityCandidate],
    relations: Sequence[RelationCandidate],
) -> list[EntityCandidate]:
    buckets: dict[str, EntityCandidate] = {item.entity_id: item for item in entities}
    for relation in relations:
        for identifier, name in (
            (relation.source_entity_id, relation.source_name),
            (relation.target_entity_id, relation.target_name),
        ):
            derived_score = relation.score * 0.8
            current = buckets.get(identifier)
            if current is None:
                buckets[identifier] = EntityCandidate(
                    entity_id=identifier,
                    canonical_name=name,
                    entity_type="OTHER",
                    score=derived_score,
                    matched_by=("relation_endpoint",),
                )
            elif derived_score > current.score:
                buckets[identifier] = EntityCandidate(
                    entity_id=current.entity_id,
                    canonical_name=current.canonical_name,
                    entity_type=current.entity_type,
                    score=derived_score,
                    matched_by=tuple(sorted(set((*current.matched_by, "relation_endpoint")))),
                )
    return sorted(
        buckets.values(),
        key=lambda item: (-item.score, item.canonical_name, item.entity_id),
    )


def _normalized_entity_score(
    item: EntityCandidate, candidates: Sequence[EntityCandidate]
) -> float:
    maximum = max((candidate.score for candidate in candidates), default=1.0)
    return item.score / maximum if maximum else 0.0


def _normalized_relation_scores(
    candidates: Sequence[RelationCandidate],
) -> dict[str, float]:
    maximum = max((candidate.score for candidate in candidates), default=1.0)
    return {
        item.relation_id: item.score / maximum if maximum else 0.0
        for item in candidates
    }


def _edge_relevance(
    edge: GraphEdge,
    query: str,
    relation_relevance: dict[str, float],
    *,
    current_entity_id: str,
    branching_factor: int = 1,
) -> float:
    score = relation_relevance.get(edge.relation_id, 0.0)
    for value, weight in (
        (edge.source_name, 0.35),
        (edge.target_name, 0.35),
        (edge.relation_type_name, 0.25),
    ):
        score += weight * _query_match_strength(value, query)
    score += min(0.2, math.log1p(edge.evidence_count) / 20)
    score -= min(0.22, math.log1p(max(0, branching_factor - 1)) / 18)
    asks_for_actor = any(cue in query for cue in ("누가", "주체", "어느 기관", "어떤 기관"))
    asks_for_target = any(cue in query for cue in ("무엇을", "누구에게", "대상", "어떤 조치"))
    if asks_for_actor and current_entity_id == edge.target_entity_id:
        score += 0.15
    if asks_for_target and current_entity_id == edge.source_entity_id:
        score += 0.15
    return score


def _reserve_preferred_relation_paths(
    paths: Sequence[GraphPath],
    *,
    preferred_relation_ids: Sequence[str],
    limit: int,
) -> list[GraphPath]:
    """Keep the best discovered path for each retrieval candidate in reranking."""

    if limit < 1:
        return []
    by_relation: dict[str, GraphPath] = {}
    preferred = set(preferred_relation_ids)
    for path in paths:
        for edge in path.edges:
            if edge.relation_id in preferred:
                by_relation.setdefault(edge.relation_id, path)
    reserved: list[GraphPath] = []
    seen_keys: set[tuple[str, ...]] = set()
    for relation_id in preferred_relation_ids:
        path = by_relation.get(relation_id)
        if path is None:
            continue
        key = tuple(edge.relation_id for edge in path.edges)
        if key not in seen_keys:
            seen_keys.add(key)
            reserved.append(path)
        if len(reserved) >= limit:
            return reserved
    for path in paths:
        key = tuple(edge.relation_id for edge in path.edges)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        reserved.append(path)
        if len(reserved) >= limit:
            break
    return reserved


_SEARCH_WORD_RE = re.compile(r"[0-9A-Za-z가-힣]+")
_SEARCH_SUFFIXES = (
    "에서는",
    "으로부터",
    "에게서",
    "에서",
    "으로",
    "에게",
    "까지",
    "부터",
    "에는",
    "과는",
    "와는",
    "이란",
    "란",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "에",
    "의",
    "과",
    "와",
    "로",
)
_SEARCH_STOP_WORDS = {
    "관한",
    "관련",
    "어떻게",
    "무엇",
    "무엇이며",
    "되는가",
    "있는가",
    "규정",
    "사항",
}


def _query_match_strength(value: str, query: str) -> float:
    normalized_value = _compact_search_text(value)
    normalized_query = _compact_search_text(query)
    if len(normalized_value) < 2 or normalized_value not in normalized_query:
        return 0.0
    return min(1.0, len(normalized_value) / 10)


def _evidence_query_relevance(path: GraphPath, query: str) -> float:
    terms = _search_terms(query)
    if not terms:
        return 0.0
    best_coverage = 0.0
    best_document_match = 0.0
    for edge in path.edges:
        for evidence in edge.evidences:
            searchable = _compact_search_text(
                " ".join(
                    (
                        evidence.document_title,
                        evidence.context_text,
                        evidence.unit_text,
                        evidence.raw_subject,
                        evidence.raw_predicate,
                        evidence.raw_object,
                    )
                )
            )
            matched = sum(
                _compact_search_text(term) in searchable for term in terms
            )
            best_coverage = max(best_coverage, matched / len(terms))
            best_document_match = max(
                best_document_match,
                _query_match_strength(evidence.document_title, query),
            )
    return best_coverage * 0.55 + best_document_match * 0.35


def _search_terms(value: str) -> tuple[str, ...]:
    terms: list[str] = []
    for match in _SEARCH_WORD_RE.finditer(value):
        term = match.group(0).lower()
        for suffix in _SEARCH_SUFFIXES:
            if term.endswith(suffix) and len(term) - len(suffix) >= 2:
                term = term[: -len(suffix)]
                break
        if len(term) >= 2 and term not in _SEARCH_STOP_WORDS and term not in terms:
            terms.append(term)
    return tuple(terms[:12])


def _compact_search_text(value: str) -> str:
    return "".join(_SEARCH_WORD_RE.findall(value.lower()))


def _state_to_path(state: _PathState) -> GraphPath:
    # Compare different hop lengths on a similar scale instead of allowing
    # longer paths to win merely because more edge scores were accumulated.
    length_penalty = 1.0 + 0.7 * len(state.edges)
    return GraphPath(
        start_entity_id=state.start_entity_id,
        start_name=state.start_name,
        end_entity_id=state.current_entity_id,
        end_name=state.current_name,
        edges=list(state.edges),
        traversed_entity_ids=list(state.entity_ids),
        traversed_entity_names=list(state.entity_names),
        score=state.score / length_penalty,
    )


def _edge_ids(edges: Sequence[GraphEdge]) -> tuple[str, ...]:
    return tuple(edge.relation_id for edge in edges)


def _deduplicate_paths(paths: Sequence[GraphPath]) -> list[GraphPath]:
    buckets: dict[tuple[tuple[str, ...], tuple[str, ...]], GraphPath] = {}
    for path in paths:
        key = (
            tuple(path.traversed_entity_ids),
            tuple(edge.relation_id for edge in path.edges),
        )
        current = buckets.get(key)
        if current is None or path.score > current.score:
            buckets[key] = path
    return sorted(
        buckets.values(),
        key=lambda item: (-item.score, item.hops, tuple(item.traversed_entity_ids)),
    )


def _unique_edges(paths: Sequence[GraphPath]) -> list[GraphEdge]:
    buckets: dict[str, GraphEdge] = {}
    for path in paths:
        for edge in path.edges:
            buckets.setdefault(edge.relation_id, edge)
    return list(buckets.values())


def _update_path_documents(path: GraphPath) -> None:
    documents: dict[str, str] = {}
    for edge in path.edges:
        for evidence in edge.evidences:
            documents[evidence.document_id] = evidence.document_title
    path.document_ids = sorted(documents)
    path.document_titles = sorted(set(documents.values()))


def _query_path_coverage_boost(path: GraphPath, query: str) -> float:
    names = {name.strip() for name in path.traversed_entity_names if len(name.strip()) >= 2}
    matched = sum(name in query for name in names)
    boost = matched * 0.18
    if "거쳐" in query and path.hops >= 2 and names and matched == len(names):
        boost += 0.45
    return boost


def _path_text(path: GraphPath) -> str:
    parts: list[str] = []
    for index, edge in enumerate(path.edges):
        if index:
            parts.append(" / ")
        parts.append(
            f"{edge.source_name} - {edge.relation_type_name} → {edge.target_name}"
        )
    documents = ", ".join(path.document_titles)
    if documents:
        return f"{''.join(parts)} (근거 문서: {documents})"
    return "".join(parts)


def _source_ref_text(source_ref: dict[str, object]) -> str:
    parts: list[str] = []
    article = source_ref.get("article")
    paragraph = source_ref.get("paragraph")
    item_path = source_ref.get("item_path")
    if article:
        parts.append(str(article))
    if paragraph not in (None, ""):
        parts.append(f"제{paragraph}항")
    if isinstance(item_path, list) and item_path:
        parts.append("-".join(str(item) for item in item_path))
    return " ".join(parts)


def _deduplicate_text(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _timeout_answer() -> str:
    return "탐색 시간 한도 안에 근거 경로를 확정하지 못했습니다."
