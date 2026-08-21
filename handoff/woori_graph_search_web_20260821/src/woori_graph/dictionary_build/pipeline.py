"""Framework-neutral dictionary-construction pipeline facade."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..candidates import build_candidate_dictionaries
from ..closed_relations import build_closed_relation_dictionary
from ..documents import segment_paths, segment_text
from ..entity_clustering import build_clustered_entity_dictionary
from ..extraction import CompletionClient, extract_units, load_prompt
from ..models import SemanticUnit
from ..normalization import build_first_pass_normalization


@dataclass(frozen=True)
class DictionaryArtifacts:
    entity_candidates: list[dict[str, Any]]
    relation_candidates: list[dict[str, Any]]
    entity_dictionary: list[dict[str, Any]]
    relation_dictionary: list[dict[str, Any]]


class DictionaryBuildPipeline:
    """The three dictionary-build steps without API or database dependencies."""

    def segment_path(self, input_path: Path) -> list[SemanticUnit]:
        return list(segment_paths(input_path))

    def segment_document(
        self,
        content: str,
        *,
        source_document_key: str,
        title_hint: str,
    ) -> list[SemanticUnit]:
        return segment_text(
            content,
            source_path=source_document_key,
            fallback_title=title_hint,
            source_document_key=source_document_key,
        )

    def extract(
        self,
        units: Sequence[SemanticUnit],
        client: CompletionClient,
        *,
        workers: int = 4,
        prompt_file: Path | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return extract_units(
            units,
            client,
            workers=workers,
            prompt_template=load_prompt(prompt_file),
        )

    def normalize(
        self,
        raw_records: Sequence[dict[str, Any]],
        *,
        entity_mapping: Mapping[str, Mapping[str, str]],
        relation_mapping: Mapping[str, Mapping[str, str]],
        closed_relation_mapping: Sequence[dict[str, Any]],
    ) -> DictionaryArtifacts:
        entity_candidates, relation_candidates = build_candidate_dictionaries(raw_records)
        initial_entities, initial_relations, _ = build_first_pass_normalization(
            raw_records,
            relation_mapping,
        )
        entities = build_clustered_entity_dictionary(
            initial_entities,
            entity_mapping,
        )
        relations = build_closed_relation_dictionary(
            initial_relations,
            closed_relation_mapping,
        )
        return DictionaryArtifacts(
            entity_candidates=entity_candidates,
            relation_candidates=relation_candidates,
            entity_dictionary=entities,
            relation_dictionary=relations,
        )
