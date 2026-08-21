"""Discover and verify questions whose graph paths cite multiple documents."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from .artifacts import _sha256, _write_json, _write_text, portable_manifest_path
from .models import Evidence, GraphEdge, SearchResult
from .pipeline import build_grounded_answer


_CITATION_NAME_RE = re.compile(r"^제?\d+(?:조|항|호|목)")
_POOR_NAMES = {
    "것",
    "경우",
    "자",
    "법",
    "영",
    "규정",
    "사항",
    "업무",
    "내용",
    "방법",
    "필요",
    "조치",
}
_POOR_MIDDLE_NAMES = _POOR_NAMES | {
    "기준",
    "세부사항",
    "절차",
    "범위",
    "대상",
    "사유",
    "자료",
    "정보",
    "요건",
}


@dataclass(frozen=True)
class CrossDocumentQuestion:
    question_id: str
    question: str
    start_entity_id: str
    start_name: str
    middle_entity_id: str
    middle_name: str
    end_entity_id: str
    end_name: str
    expected_relation_ids: tuple[str, str]
    expected_document_ids: tuple[str, str]
    expected_document_titles: tuple[str, str]
    discovery_score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def discover_cross_document_questions(
    edges: Sequence[GraphEdge],
    *,
    count: int = 10,
) -> list[CrossDocumentQuestion]:
    adjacency: dict[str, list[GraphEdge]] = defaultdict(list)
    names: dict[str, str] = {}
    for edge in edges:
        if not edge.evidences:
            continue
        adjacency[edge.source_entity_id].append(edge)
        adjacency[edge.target_entity_id].append(edge)
        names[edge.source_entity_id] = edge.source_name
        names[edge.target_entity_id] = edge.target_name

    candidates: list[tuple[float, str, GraphEdge, Evidence, GraphEdge, Evidence]] = []
    for middle_id, incident in adjacency.items():
        middle_name = names[middle_id]
        if (
            not _quality_name(middle_name)
            or middle_name in _POOR_MIDDLE_NAMES
            or len(incident) < 2
            or len(incident) > 80
        ):
            continue
        ranked_incident = sorted(
            incident,
            key=lambda edge: (-edge.evidence_count, edge.relation_id),
        )[:30]
        for left, right in itertools.combinations(ranked_incident, 2):
            start_id = left.other_entity_id(middle_id)
            end_id = right.other_entity_id(middle_id)
            if start_id == end_id:
                continue
            start_name = left.entity_name(start_id)
            end_name = right.entity_name(end_id)
            if not _quality_name(start_name) or not _quality_name(end_name):
                continue
            evidence_pair = _different_document_evidence(left, right)
            if evidence_pair is None:
                continue
            left_evidence, right_evidence = evidence_pair
            lexical_diversity = len(
                {start_name, middle_name, end_name, left.relation_type_name, right.relation_type_name}
            )
            score = (
                2.0
                + math.log1p(left.evidence_count)
                + math.log1p(right.evidence_count)
                + lexical_diversity * 0.15
                - math.log1p(len(incident)) * 0.3
            )
            candidates.append(
                (score, middle_id, left, left_evidence, right, right_evidence)
            )

    selected: list[CrossDocumentQuestion] = []
    used_relation_pairs: set[tuple[str, str]] = set()
    used_endpoint_pairs: set[tuple[str, str]] = set()
    used_document_pairs: dict[tuple[str, str], int] = defaultdict(int)
    used_middle_entities: set[str] = set()
    for score, middle_id, left, left_evidence, right, right_evidence in sorted(
        candidates,
        key=lambda item: (-item[0], item[1], item[2].relation_id, item[4].relation_id),
    ):
        relation_pair = tuple(sorted((left.relation_id, right.relation_id)))
        if relation_pair in used_relation_pairs:
            continue
        if middle_id in used_middle_entities:
            continue
        start_id = left.other_entity_id(middle_id)
        end_id = right.other_entity_id(middle_id)
        endpoint_pair = tuple(sorted((start_id, end_id)))
        if endpoint_pair in used_endpoint_pairs:
            continue
        document_pair = tuple(
            sorted((left_evidence.document_id, right_evidence.document_id))
        )
        if used_document_pairs[document_pair] >= 2:
            continue
        start_name = left.entity_name(start_id)
        middle_name = left.entity_name(middle_id)
        end_name = right.entity_name(end_id)
        question = (
            f"‘{start_name}’와 ‘{end_name}’는 ‘{middle_name}’를 거쳐 어떻게 "
            "연결되며, 각 연결의 근거 법령은 무엇인가?"
        )
        digest = hashlib.sha256(
            "|".join(relation_pair).encode("utf-8")
        ).hexdigest()[:10]
        selected.append(
            CrossDocumentQuestion(
                question_id=f"crossdoc-{len(selected) + 1:02d}-{digest}",
                question=question,
                start_entity_id=start_id,
                start_name=start_name,
                middle_entity_id=middle_id,
                middle_name=middle_name,
                end_entity_id=end_id,
                end_name=end_name,
                expected_relation_ids=(left.relation_id, right.relation_id),
                expected_document_ids=(
                    left_evidence.document_id,
                    right_evidence.document_id,
                ),
                expected_document_titles=(
                    left_evidence.document_title,
                    right_evidence.document_title,
                ),
                discovery_score=round(score, 8),
            )
        )
        used_relation_pairs.add(relation_pair)
        used_endpoint_pairs.add(endpoint_pair)
        used_document_pairs[document_pair] += 1
        used_middle_entities.add(middle_id)
        if len(selected) >= count:
            break
    if len(selected) < count:
        raise RuntimeError(
            f"only {len(selected)} suitable cross-document questions were found; requested {count}"
        )
    return selected


def evaluate_cross_document_result(
    question: CrossDocumentQuestion,
    result: SearchResult,
) -> dict[str, Any]:
    expected = set(question.expected_relation_ids)
    exact_path = None
    entity_path = None
    required_entities = {
        question.start_entity_id,
        question.middle_entity_id,
        question.end_entity_id,
    }
    for path in result.paths:
        relation_ids = {edge.relation_id for edge in path.edges}
        if expected.issubset(relation_ids) and len(path.document_ids) >= 2:
            exact_path = path
            break
        if (
            entity_path is None
            and required_entities.issubset(set(path.traversed_entity_ids))
            and len(path.document_ids) >= 2
        ):
            entity_path = path
    matched_path = exact_path or entity_path
    answer = (
        build_grounded_answer(
            question.question,
            [matched_path],
            timed_out=result.stats.timed_out,
            path_limit=1,
        )
        if matched_path is not None
        else result.answer
    )
    return {
        "question": question.to_dict(),
        "answer": answer,
        "search_stats": result.stats.to_dict(),
        "expected_path_found": matched_path is not None,
        "match_kind": (
            "exact_relations"
            if exact_path is not None
            else "same_entities_alternative_relations"
            if entity_path is not None
            else None
        ),
        "matched_path": matched_path.to_dict() if matched_path is not None else None,
    }


def write_cross_document_qa_artifacts(
    records: Sequence[dict[str, Any]],
    *,
    output_dir: Path,
    config_path: Path,
    embedding_model: str,
    embedding_dimension: int,
    overwrite: bool = False,
) -> dict[str, Any]:
    final_dir = output_dir / "final"
    work_dir = output_dir / "work"
    final_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        work_dir / "01_discovered_questions.json",
        [record["question"] for record in records],
        overwrite=overwrite,
    )
    jsonl = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    _write_text(final_dir / "questions_answers.jsonl", jsonl, overwrite=overwrite)
    markdown: list[str] = ["# 문서 간 연결 질문과 답변", ""]
    for index, record in enumerate(records, start=1):
        question = record["question"]["question"]
        markdown.extend(
            (
                f"## {index}. {question}",
                "",
                record["answer"],
                "",
                f"검증: expected_path_found={str(record['expected_path_found']).lower()}",
                "",
            )
        )
    _write_text(
        final_dir / "questions_answers.md",
        "\n".join(markdown),
        overwrite=overwrite,
    )
    passed_count = sum(bool(record["expected_path_found"]) for record in records)
    manifest_path = final_dir / "qa_manifest.json"
    generated_files = sorted(
        path
        for path in output_dir.rglob("*")
        if path.is_file() and path != manifest_path
    )
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "embedding": {
            "model": embedding_model,
            "dimension": embedding_dimension,
        },
        "config": {
            "path": portable_manifest_path(config_path),
            "sha256": _sha256(config_path.resolve()),
        },
        "question_count": len(records),
        "expected_path_found_count": passed_count,
        "passed": len(records) == 10 and passed_count == len(records),
        "files": [
            {
                "path": path.relative_to(output_dir).as_posix(),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in generated_files
        ],
    }
    _write_json(manifest_path, manifest, overwrite=overwrite)
    return manifest


def _different_document_evidence(
    left: GraphEdge, right: GraphEdge
) -> tuple[Evidence, Evidence] | None:
    for left_evidence in left.evidences:
        for right_evidence in right.evidences:
            if left_evidence.document_id != right_evidence.document_id:
                return left_evidence, right_evidence
    return None


def _quality_name(name: str) -> bool:
    normalized = name.strip()
    return (
        2 <= len(normalized) <= 60
        and normalized not in _POOR_NAMES
        and "�" not in normalized
        and not normalized.isdigit()
        and not _CITATION_NAME_RE.match(normalized)
    )
