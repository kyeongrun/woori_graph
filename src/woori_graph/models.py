"""Typed records exchanged by the v3 JSONL stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class SourceRef:
    """A reproducible location in a source regulation."""

    article: str
    paragraph: int | None
    item_path: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "article": self.article,
            "paragraph": self.paragraph,
            "item_path": list(self.item_path),
        }


@dataclass(frozen=True)
class SemanticUnit:
    """The smallest extraction request with its required source context."""

    semantic_unit_id: str
    document_id: str
    document_title: str
    source_path: str
    source_ref: SourceRef
    context_text: str
    unit_text: str
    unit_kind: str
    governing_text: str = ""
    resolved_text: str = ""
    resolution_type: str = "UNRESOLVED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_unit_id": self.semantic_unit_id,
            "document_id": self.document_id,
            "document_title": self.document_title,
            "source_path": self.source_path,
            "source_ref": self.source_ref.to_dict(),
            "context_text": self.context_text,
            "unit_text": self.unit_text,
            "unit_kind": self.unit_kind,
            "governing_text": self.governing_text,
            "resolved_text": self.resolved_text,
            "resolution_type": self.resolution_type,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SemanticUnit":
        source_ref = value["source_ref"]
        return cls(
            semantic_unit_id=value["semantic_unit_id"],
            document_id=value["document_id"],
            document_title=value["document_title"],
            source_path=value.get("source_path", ""),
            source_ref=SourceRef(
                article=source_ref["article"],
                paragraph=source_ref.get("paragraph"),
                item_path=tuple(source_ref.get("item_path", [])),
            ),
            context_text=value["context_text"],
            unit_text=value["unit_text"],
            unit_kind=value.get("unit_kind", "unknown"),
            governing_text=value.get("governing_text", ""),
            resolved_text=value.get("resolved_text", ""),
            resolution_type=value.get("resolution_type", "UNRESOLVED"),
        )


@dataclass(frozen=True)
class RawRelation:
    """An explicitly expressed, unnormalised SVO relation."""

    relation_mention_id: str
    subject: str
    predicate: str
    object: str

    def to_dict(self) -> dict[str, str]:
        return {
            "relation_mention_id": self.relation_mention_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
        }
