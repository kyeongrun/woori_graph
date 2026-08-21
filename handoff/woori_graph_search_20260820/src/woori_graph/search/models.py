"""Store-neutral models for hybrid graph search and grounded answers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class EntityCandidate:
    entity_id: str
    canonical_name: str
    entity_type: str
    score: float
    matched_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelationCandidate:
    relation_id: str
    source_entity_id: str
    source_name: str
    relation_type_id: str
    relation_type_name: str
    target_entity_id: str
    target_name: str
    score: float
    matched_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Evidence:
    relation_mention_id: str
    relation_id: str
    semantic_unit_id: str
    document_id: str
    document_title: str
    source_path: str
    source_ref: dict[str, Any]
    raw_subject: str
    raw_predicate: str
    raw_object: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphEdge:
    relation_id: str
    source_entity_id: str
    source_name: str
    relation_type_id: str
    relation_type_name: str
    polarity: str | None
    target_entity_id: str
    target_name: str
    evidence_count: int
    evidences: list[Evidence] = field(default_factory=list)

    def other_entity_id(self, entity_id: str) -> str:
        if entity_id == self.source_entity_id:
            return self.target_entity_id
        if entity_id == self.target_entity_id:
            return self.source_entity_id
        raise ValueError(f"entity {entity_id} is not incident to relation {self.relation_id}")

    def entity_name(self, entity_id: str) -> str:
        if entity_id == self.source_entity_id:
            return self.source_name
        if entity_id == self.target_entity_id:
            return self.target_name
        raise ValueError(f"entity {entity_id} is not incident to relation {self.relation_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "source_entity_id": self.source_entity_id,
            "source_name": self.source_name,
            "relation_type_id": self.relation_type_id,
            "relation_type_name": self.relation_type_name,
            "polarity": self.polarity,
            "target_entity_id": self.target_entity_id,
            "target_name": self.target_name,
            "evidence_count": self.evidence_count,
            "evidences": [evidence.to_dict() for evidence in self.evidences],
        }


@dataclass
class GraphPath:
    start_entity_id: str
    start_name: str
    end_entity_id: str
    end_name: str
    edges: list[GraphEdge]
    traversed_entity_ids: list[str]
    traversed_entity_names: list[str]
    score: float
    document_ids: list[str] = field(default_factory=list)
    document_titles: list[str] = field(default_factory=list)

    @property
    def hops(self) -> int:
        return len(self.edges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_entity_id": self.start_entity_id,
            "start_name": self.start_name,
            "end_entity_id": self.end_entity_id,
            "end_name": self.end_name,
            "hops": self.hops,
            "score": round(self.score, 8),
            "document_ids": self.document_ids,
            "document_titles": self.document_titles,
            "traversed_entity_ids": self.traversed_entity_ids,
            "traversed_entity_names": self.traversed_entity_names,
            "edges": [edge.to_dict() for edge in self.edges],
        }


@dataclass(frozen=True)
class SearchStats:
    duration_seconds: float
    timed_out: bool
    max_hops_requested: int
    max_hops_reached: int
    entity_candidates: int
    relation_candidates: int
    paths_considered: int
    paths_returned: int
    adaptations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SearchResult:
    query: str
    answer: str
    entity_candidates: list[EntityCandidate]
    relation_candidates: list[RelationCandidate]
    paths: list[GraphPath]
    stats: SearchStats

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "answer": self.answer,
            "entity_candidates": [item.to_dict() for item in self.entity_candidates],
            "relation_candidates": [item.to_dict() for item in self.relation_candidates],
            "paths": [path.to_dict() for path in self.paths],
            "stats": self.stats.to_dict(),
        }

