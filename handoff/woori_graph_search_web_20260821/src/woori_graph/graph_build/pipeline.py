"""Framework-neutral graph-construction pipeline facade."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..documents import segment_paths, segment_text
from ..extraction import CompletionClient, extract_units, load_prompt
from ..graph_mapping import (
    GraphLoadBundle,
    UnmappedRelationError,
    collect_unmapped_predicates,
    map_raw_svo_to_graph,
)
from ..models import SemanticUnit
from .relation_mapper import propose_forced_relation_overrides


class GraphBuildPipeline:
    """The four graph-build steps without API or database dependencies."""

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

    def map_for_load(
        self,
        raw_records: Sequence[dict[str, Any]],
        entity_dictionary: Sequence[dict[str, Any]],
        relation_dictionary: Sequence[dict[str, Any]],
        *,
        dictionary_version: str,
        client: CompletionClient | None = None,
        relation_workers: int = 4,
    ) -> GraphLoadBundle:
        missing = collect_unmapped_predicates(raw_records, relation_dictionary)
        overrides: dict[str, str] = {}
        if missing:
            if client is None:
                raise UnmappedRelationError(missing)
            overrides, errors = propose_forced_relation_overrides(
                missing,
                relation_dictionary,
                client,
                workers=relation_workers,
            )
            if errors:
                raise UnmappedRelationError(
                    sorted(set(missing) - set(overrides))
                )
        return map_raw_svo_to_graph(
            raw_records,
            entity_dictionary,
            relation_dictionary,
            dictionary_version=dictionary_version,
            relation_overrides=overrides,
        )
