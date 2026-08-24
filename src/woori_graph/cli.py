"""Command line entry points for the v3 dictionary-building stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .candidates import build_candidate_dictionaries, build_simple_surface_lists
from .closed_relations import (
    audit_closed_relation_dictionary,
    build_closed_relation_dictionary,
    propose_closed_relation_mapping,
    propose_relation_taxonomy,
    sanitize_closed_relation_mapping,
)
from .context_resolution import (
    DEFAULT_CONTEXT_RESOLUTION_PROMPT,
    audit_context_resolution,
    normalize_context_resolution_records,
    resolve_units as resolve_context_units,
)
from .contextual_entities import (
    CONTEXTUAL_ENTITY_NORMALIZATION_PROMPT,
    audit_contextual_entity_dictionary,
    build_contextually_normalized_entity_dictionary,
    entity_lexical_key,
    is_acceptable_llm_canonical_mapping,
    needs_contextual_entity_normalization,
    propose_contextual_entity_mapping,
    rekey_llm_mapping_by_source_name,
    sanitize_contextual_entity_mapping,
)
from .audit import audit_artifacts, audit_dictionary_release, audit_raw_svo
from .documents import build_source_manifest, segment_paths
from .embeddings import EmbeddingConfig, OpenAICompatEmbeddingClient
from .environment import load_env_file
from .dictionary_build import (
    build_refreshed_relation_dictionary,
    build_seeded_entity_mapping,
)
from .entity_clustering import (
    build_clustered_entity_dictionary,
    entity_mapping_from_records,
    entity_mapping_records,
    propose_entity_mapping,
)
from .entity_typing import (
    apply_entity_type_mapping,
    audit_entity_types,
    build_entity_type_mapping,
    merge_entity_type_mappings,
    propose_llm_entity_types,
)
from .extraction import (
    OpenAICompatClient,
    OpenAICompatConfig,
    extract_units,
    align_raw_svo_records,
    load_prompt,
    sanitize_raw_svo_records,
)
from .graph_build.relation_mapper import propose_forced_relation_overrides
from .graph_build.ingest import (
    load_document_ingest_config,
    run_document_ingest,
)
from .graph_mapping import build_relation_alias_index
from .jsonl import append_jsonl, read_jsonl, write_jsonl
from .models import SemanticUnit
from .prompting import DEFAULT_PROMPT_ROOT
from .normalization import (
    build_first_pass_normalization,
    propose_relation_mapping,
    relation_mapping_from_records,
    relation_mapping_records,
)
from .storage import build_storage_load_files, relation_overrides_from_records
from .search.application import SearchApplication
from .search.config import load_search_config
from .search.cross_document import (
    discover_cross_document_questions,
    evaluate_cross_document_result,
    write_cross_document_qa_artifacts,
)
from .search.web import create_search_web_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="woori-graph", description="v3 SVO dictionary-building pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    segment = subparsers.add_parser("segment", help="split law Markdown into semantic-units JSONL")
    segment.add_argument("--input", type=Path, required=True, help="Markdown file or directory")
    segment.add_argument("--output", type=Path, required=True, help="semantic-units JSONL path")
    segment.add_argument(
        "--source-manifest-output",
        type=Path,
        help="optional source document hash manifest JSONL path",
    )
    segment.add_argument("--overwrite", action="store_true")

    resolve_context = subparsers.add_parser(
        "resolve-context",
        help="turn segmented units into standalone extraction text through the LLM",
    )
    resolve_context.add_argument("--units", type=Path, required=True)
    resolve_context.add_argument("--output", type=Path, required=True)
    resolve_context.add_argument("--errors-output", type=Path)
    resolve_context.add_argument("--prompt-file", type=Path)
    resolve_context.add_argument("--workers", type=int, default=None)
    resolve_context.add_argument("--offset", type=int, default=0)
    resolve_context.add_argument("--limit", type=int)
    resolve_context.add_argument("--batch-size", type=int, default=200)
    resolve_context.add_argument("--resume", action="store_true")
    resolve_context.add_argument("--allow-remote-llm", action="store_true")
    resolve_context.add_argument("--env-file", type=Path)
    resolve_context.add_argument("--overwrite", action="store_true")

    audit_context = subparsers.add_parser(
        "audit-context",
        help="audit coverage and source preservation of context-resolved units",
    )
    audit_context.add_argument("--source-units", type=Path, required=True)
    audit_context.add_argument("--resolved-units", type=Path, required=True)
    audit_context.add_argument("--output", type=Path, required=True)
    audit_context.add_argument("--offset", type=int, default=0)
    audit_context.add_argument("--limit", type=int)
    audit_context.add_argument("--overwrite", action="store_true")

    normalize_context = subparsers.add_parser(
        "normalize-context",
        help="derive COPIED/CONTEXT_INHERITED from unit_text and resolved_text",
    )
    normalize_context.add_argument("--input", type=Path, required=True)
    normalize_context.add_argument("--output", type=Path, required=True)
    normalize_context.add_argument("--overwrite", action="store_true")

    extract = subparsers.add_parser("extract", help="extract raw SVO records through the local LLM")
    extract.add_argument("--units", type=Path, required=True, help="semantic-units JSONL path")
    extract.add_argument("--output", type=Path, required=True, help="raw SVO JSONL path")
    extract.add_argument("--errors-output", type=Path, help="failed extraction JSONL path")
    extract.add_argument("--prompt-file", type=Path, help="optional raw SVO prompt override")
    extract.add_argument(
        "--preserve-llm-output",
        action="store_true",
        help="preserve LLM subject/predicate/object text without endpoint sanitizing or splitting",
    )
    extract.add_argument("--workers", type=int, default=None, help="defaults to VLLM_CONCURRENCY or 4")
    extract.add_argument("--offset", type=int, default=0, help="skip the first N units (pilot/resume helper)")
    extract.add_argument("--limit", type=int, help="extract only the first N units")
    extract.add_argument("--batch-size", type=int, default=200, help="persist progress after each N units")
    extract.add_argument("--resume", action="store_true", help="skip semantic-unit IDs already present in output")
    extract.add_argument("--allow-remote-llm", action="store_true", help="allow the configured non-loopback endpoint")
    extract.add_argument("--env-file", type=Path, help="optional .env file; values do not replace existing env vars")
    extract.add_argument("--overwrite", action="store_true")

    candidates = subparsers.add_parser("candidates", help="make exact-surface review dictionaries from raw SVO JSONL")
    candidates.add_argument("--raw-svo", type=Path, required=True)
    candidates.add_argument("--entities-output", type=Path, required=True)
    candidates.add_argument("--relations-output", type=Path, required=True)
    candidates.add_argument("--sample-limit", type=int, default=5)
    candidates.add_argument(
        "--simple-with-source",
        action="store_true",
        help="write one exact-surface row with its first unit_text source and mention count",
    )
    candidates.add_argument("--overwrite", action="store_true")

    normalize = subparsers.add_parser("normalize", help="build conservative first-pass dictionaries and edges")
    normalize.add_argument("--raw-svo", type=Path, required=True)
    normalize.add_argument("--entities-output", type=Path, required=True)
    normalize.add_argument("--relations-output", type=Path, required=True)
    normalize.add_argument("--edges-output", type=Path, required=True)
    normalize.add_argument("--relation-map-output", type=Path, required=True)
    normalize.add_argument(
        "--relation-map-input",
        type=Path,
        help="reuse a generated or human-reviewed relation map without calling the LLM",
    )
    normalize.add_argument(
        "--retry-fallback",
        action="store_true",
        help="with --relation-map-input, ask the LLM again only for fallback aliases",
    )
    normalize.add_argument("--errors-output", type=Path, required=True)
    normalize.add_argument("--env-file", type=Path)
    normalize.add_argument("--allow-remote-llm", action="store_true")
    normalize.add_argument("--relation-batch-size", type=int, default=80)
    normalize.add_argument("--relation-workers", type=int, default=4)
    normalize.add_argument("--sample-limit", type=int, default=5)
    normalize.add_argument("--overwrite", action="store_true")

    merge = subparsers.add_parser("merge-jsonl", help="merge ordered JSONL shards with optional ID de-duplication")
    merge.add_argument("--inputs", type=Path, nargs="+", required=True)
    merge.add_argument("--output", type=Path, required=True)
    merge.add_argument("--dedupe-key", help="optional top-level field used to remove duplicate records")
    merge.add_argument("--overwrite", action="store_true")

    align_svo = subparsers.add_parser(
        "align-svo",
        help="merge raw-SVO files, require exact semantic-unit coverage, and restore source order",
    )
    align_svo.add_argument("--units", type=Path, required=True)
    align_svo.add_argument("--inputs", type=Path, nargs="+", required=True)
    align_svo.add_argument("--output", type=Path, required=True)
    align_svo.add_argument("--overwrite", action="store_true")

    audit_svo = subparsers.add_parser("audit-svo", help="verify a standalone semantic-unit/raw-SVO run")
    audit_svo.add_argument("--units", type=Path, required=True)
    audit_svo.add_argument("--raw-svo", type=Path, required=True)
    audit_svo.add_argument("--output", type=Path, required=True)
    audit_svo.add_argument("--sample-limit", type=int, default=20)
    audit_svo.add_argument("--overwrite", action="store_true")

    audit_dictionaries = subparsers.add_parser(
        "audit-dictionaries",
        help="verify a final entity and closed relation dictionary release",
    )
    audit_dictionaries.add_argument("--raw-svo", type=Path, required=True)
    audit_dictionaries.add_argument("--entities", type=Path, required=True)
    audit_dictionaries.add_argument("--relations", type=Path, required=True)
    audit_dictionaries.add_argument("--output", type=Path, required=True)
    audit_dictionaries.add_argument(
        "--expected-relation-types", type=int, default=98
    )
    audit_dictionaries.add_argument("--overwrite", action="store_true")

    sanitize_svo = subparsers.add_parser(
        "sanitize-svo",
        help="reapply deterministic endpoint cleanup to stored raw SVO JSONL",
    )
    sanitize_svo.add_argument("--input", type=Path, required=True)
    sanitize_svo.add_argument("--output", type=Path, required=True)
    sanitize_svo.add_argument(
        "--units",
        type=Path,
        help="validate exact coverage and restore semantic-unit source order",
    )
    sanitize_svo.add_argument("--overwrite", action="store_true")

    seed_entities = subparsers.add_parser(
        "seed-entity-map",
        help="reuse only unambiguous aliases from an earlier entity dictionary",
    )
    seed_entities.add_argument("--entities-input", type=Path, required=True)
    seed_entities.add_argument("--seed-dictionary", type=Path, required=True)
    seed_entities.add_argument("--map-output", type=Path, required=True)
    seed_entities.add_argument("--overwrite", action="store_true")

    refresh_relations = subparsers.add_parser(
        "refresh-relations",
        help="force all raw predicates into an existing closed relation taxonomy",
    )
    refresh_relations.add_argument("--raw-svo", type=Path, required=True)
    refresh_relations.add_argument("--seed-dictionary", type=Path, required=True)
    refresh_relations.add_argument("--map-output", type=Path, required=True)
    refresh_relations.add_argument("--map-input", type=Path)
    refresh_relations.add_argument("--dictionary-output", type=Path, required=True)
    refresh_relations.add_argument("--errors-output", type=Path, required=True)
    refresh_relations.add_argument("--env-file", type=Path)
    refresh_relations.add_argument("--allow-remote-llm", action="store_true")
    refresh_relations.add_argument("--batch-size", type=int, default=40)
    refresh_relations.add_argument("--workers", type=int, default=4)
    refresh_relations.add_argument("--max-tokens", type=int, default=4096)
    refresh_relations.add_argument("--overwrite", action="store_true")

    cluster_entities = subparsers.add_parser(
        "cluster-entities", help="cluster entity surface forms into canonical-name dictionaries"
    )
    cluster_entities.add_argument("--entities-input", type=Path, required=True)
    cluster_entities.add_argument("--dictionary-output", type=Path, required=True)
    cluster_entities.add_argument("--map-output", type=Path, required=True)
    cluster_entities.add_argument("--map-input", type=Path)
    cluster_entities.add_argument("--retry-fallback", action="store_true")
    cluster_entities.add_argument("--errors-output", type=Path, required=True)
    cluster_entities.add_argument("--env-file", type=Path)
    cluster_entities.add_argument("--allow-remote-llm", action="store_true")
    cluster_entities.add_argument("--batch-size", type=int, default=50)
    cluster_entities.add_argument("--workers", type=int, default=4)
    cluster_entities.add_argument("--offset", type=int, default=0)
    cluster_entities.add_argument("--limit", type=int)
    cluster_entities.add_argument("--sample-limit", type=int, default=5)
    cluster_entities.add_argument("--overwrite", action="store_true")

    compress_relations = subparsers.add_parser(
        "compress-relations",
        help="map an existing relation dictionary into at most 100 closed relation types",
    )
    compress_relations.add_argument("--relations-input", type=Path, required=True)
    compress_relations.add_argument("--taxonomy-output", type=Path, required=True)
    compress_relations.add_argument("--taxonomy-input", type=Path)
    compress_relations.add_argument("--map-output", type=Path, required=True)
    compress_relations.add_argument("--map-input", type=Path)
    compress_relations.add_argument("--dictionary-output", type=Path, required=True)
    compress_relations.add_argument("--audit-output", type=Path, required=True)
    compress_relations.add_argument("--errors-output", type=Path, required=True)
    compress_relations.add_argument("--target-families", type=int, default=45)
    compress_relations.add_argument("--taxonomy-batch-size", type=int, default=160)
    compress_relations.add_argument("--taxonomy-workers", type=int, default=4)
    compress_relations.add_argument("--batch-size", type=int, default=50)
    compress_relations.add_argument("--workers", type=int, default=4)
    compress_relations.add_argument("--max-tokens", type=int, default=8192)
    compress_relations.add_argument("--env-file", type=Path)
    compress_relations.add_argument("--allow-remote-llm", action="store_true")
    compress_relations.add_argument("--overwrite", action="store_true")

    renormalize_entities = subparsers.add_parser(
        "renormalize-entities",
        help="perform a context-aware second normalization pass over an entity dictionary",
    )
    renormalize_entities.add_argument("--entities-input", type=Path, required=True)
    renormalize_entities.add_argument("--raw-svo", type=Path, required=True)
    renormalize_entities.add_argument("--map-output", type=Path, required=True)
    renormalize_entities.add_argument("--map-input", type=Path)
    renormalize_entities.add_argument(
        "--seed-llm-map-input",
        type=Path,
        help="reuse prior LLM canonical decisions for identical source names under current source IDs",
    )
    renormalize_entities.add_argument("--dictionary-output", type=Path, required=True)
    renormalize_entities.add_argument("--audit-output", type=Path, required=True)
    renormalize_entities.add_argument("--errors-output", type=Path, required=True)
    renormalize_entities.add_argument("--batch-size", type=int, default=40)
    renormalize_entities.add_argument("--workers", type=int, default=4)
    renormalize_entities.add_argument("--sample-limit", type=int, default=2)
    renormalize_entities.add_argument("--max-tokens", type=int, default=4096)
    renormalize_entities.add_argument("--prompt-file", type=Path)
    renormalize_entities.add_argument(
        "--prompt-suffix-file",
        type=Path,
        help="optional prompt-file supplement for strict retry feedback",
    )
    renormalize_entities.add_argument("--env-file", type=Path)
    renormalize_entities.add_argument("--allow-remote-llm", action="store_true")
    renormalize_entities.add_argument(
        "--resume", action="store_true", help="reuse completed entity mappings"
    )
    renormalize_entities.add_argument(
        "--retry-fallback",
        action="store_true",
        help="with --resume, retry mappings previously preserved after a batch error",
    )
    renormalize_entities.add_argument(
        "--candidates-only",
        action="store_true",
        help="send only long/conditional/citation and lexical-duplicate names to the LLM",
    )
    renormalize_entities.add_argument(
        "--require-all-llm",
        action="store_true",
        help=(
            "require every source entity canonical_name to come from the LLM; "
            "batch fallback, deterministic guards, and explicit overrides are disabled"
        ),
    )
    renormalize_entities.add_argument("--overwrite", action="store_true")

    classify_entity_types = subparsers.add_parser(
        "classify-entity-types",
        help="assign five entity types after canonical-name normalization",
    )
    classify_entity_types.add_argument("--entities-input", type=Path, required=True)
    classify_entity_types.add_argument("--dictionary-output", type=Path, required=True)
    classify_entity_types.add_argument("--map-output", type=Path, required=True)
    classify_entity_types.add_argument("--audit-output", type=Path, required=True)
    classify_entity_types.add_argument("--errors-output", type=Path, required=True)
    classify_entity_types.add_argument("--prompt-file", type=Path)
    classify_entity_types.add_argument("--env-file", type=Path)
    classify_entity_types.add_argument("--allow-remote-llm", action="store_true")
    classify_entity_types.add_argument("--batch-size", type=int, default=80)
    classify_entity_types.add_argument("--workers", type=int, default=4)
    classify_entity_types.add_argument("--max-tokens", type=int, default=4096)
    classify_entity_types.add_argument(
        "--resume", action="store_true", help="reuse completed LLM type mappings"
    )
    classify_entity_types.add_argument(
        "--rules-only",
        action="store_true",
        help="legacy diagnostic mode: classify with deterministic rules and do not call an LLM",
    )
    classify_entity_types.add_argument("--overwrite", action="store_true")

    build_load_files = subparsers.add_parser(
        "build-load-files",
        help="build versioned RDB, Apache AGE, and OpenSearch load artifacts",
    )
    build_load_files.add_argument("--raw-svo", type=Path, required=True)
    build_load_files.add_argument("--entities", type=Path, required=True)
    build_load_files.add_argument("--relations", type=Path, required=True)
    build_load_files.add_argument(
        "--relation-map",
        type=Path,
        help="optional forced relation map for predicates absent from the dictionary aliases",
    )
    build_load_files.add_argument("--dictionary-version", required=True)
    build_load_files.add_argument(
        "--age-graph-name",
        default="svo",
        help="AGE graph name (default: svo)",
    )
    build_load_files.add_argument("--output", type=Path, required=True)
    build_load_files.add_argument(
        "--env-file",
        type=Path,
        help="embedding environment file; vectors are generated by default",
    )
    build_load_files.add_argument("--allow-remote-embedding", action="store_true")
    build_load_files.add_argument(
        "--embedding-batch-size",
        type=int,
        help="override EMBEDDING_BATCH_SIZE",
    )
    build_load_files.add_argument(
        "--without-embeddings",
        action="store_true",
        help="diagnostic compatibility mode; standard loads should not use this",
    )
    build_load_files.add_argument("--overwrite", action="store_true")

    document_ingest = subparsers.add_parser(
        "document-ingest",
        help="run reusable new-document segmentation, SVO extraction, and dictionary mapping",
    )
    document_ingest.add_argument("--config", type=Path, required=True)
    document_ingest.add_argument("--allow-remote-llm", action="store_true")
    document_ingest.add_argument("--allow-remote-embedding", action="store_true")
    document_ingest.add_argument("--overwrite", action="store_true")

    search_query = subparsers.add_parser(
        "search-query",
        help="run hybrid entity/relation retrieval and bounded three-hop graph search",
    )
    search_query.add_argument("--config", type=Path, required=True)
    search_query.add_argument("--query", required=True)
    search_query.add_argument("--run-id")
    search_query.add_argument("--allow-remote-embedding", action="store_true")
    search_query.add_argument("--allow-remote-llm", action="store_true")
    search_query.add_argument("--overwrite", action="store_true")

    search_web = subparsers.add_parser(
        "search-web",
        help="serve an interactive local page for questions, answers, graph, and search diagnostics",
    )
    search_web.add_argument("--config", type=Path, required=True)
    search_web.add_argument("--host", default="127.0.0.1")
    search_web.add_argument("--port", type=int, default=8765)
    search_web.add_argument("--allow-remote-embedding", action="store_true")
    search_web.add_argument("--allow-remote-llm", action="store_true")
    search_web.add_argument("--open-browser", action="store_true")

    cross_document_qa = subparsers.add_parser(
        "search-cross-document-qa",
        help="discover and verify ten questions spanning multiple source documents",
    )
    cross_document_qa.add_argument("--config", type=Path, required=True)
    cross_document_qa.add_argument(
        "--run-id",
        default="cross-document-qa",
        help="combined QA artifact directory name",
    )
    cross_document_qa.add_argument("--count", type=int, default=10)
    cross_document_qa.add_argument("--allow-remote-embedding", action="store_true")
    cross_document_qa.add_argument("--allow-remote-llm", action="store_true")
    cross_document_qa.add_argument("--overwrite", action="store_true")

    audit = subparsers.add_parser("audit", help="verify coverage, UUIDs, and normalized references")
    audit.add_argument("--units", type=Path, required=True)
    audit.add_argument("--raw-svo", type=Path, required=True)
    audit.add_argument("--entities", type=Path, required=True)
    audit.add_argument("--relations", type=Path, required=True)
    audit.add_argument("--edges", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "search-web":
        search_config = load_search_config(args.config)
        if search_config.env_file:
            _load_env_file(search_config.env_file)
        with SearchApplication(
            search_config,
            allow_remote_embedding=args.allow_remote_embedding,
            allow_remote_llm=args.allow_remote_llm,
        ) as application:
            server = create_search_web_server(
                application,
                host=args.host,
                port=args.port,
            )
            actual_port = server.server_address[1]
            display_host = f"[{args.host}]" if ":" in args.host else args.host
            url = f"http://{display_host}:{actual_port}/"
            print(f"Interactive graph search: {url}", flush=True)
            if args.open_browser:
                import webbrowser

                webbrowser.open(url)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("Stopping interactive graph search.")
            finally:
                server.server_close()
        return 0

    if args.command == "search-query":
        search_config = load_search_config(args.config)
        if search_config.env_file:
            _load_env_file(search_config.env_file)
        with SearchApplication(
            search_config,
            allow_remote_embedding=args.allow_remote_embedding,
            allow_remote_llm=args.allow_remote_llm,
        ) as application:
            result, manifest = application.search_and_write(
                args.query,
                config_path=args.config,
                run_id=args.run_id,
                overwrite=args.overwrite,
            )
        print(
            f"Completed graph search {manifest['run_id']}; "
            f"paths={len(result.paths)}; "
            f"max_hops={result.stats.max_hops_reached}; "
            f"duration={result.stats.duration_seconds:.3f}s; "
            f"passed={manifest['passed']}"
        )
        return 0 if manifest["passed"] else 1

    if args.command == "search-cross-document-qa":
        if args.count != 10:
            raise ValueError("search-cross-document-qa currently requires --count 10")
        search_config = load_search_config(args.config)
        if search_config.env_file:
            _load_env_file(search_config.env_file)
        with SearchApplication(
            search_config,
            allow_remote_embedding=args.allow_remote_embedding,
            allow_remote_llm=args.allow_remote_llm,
        ) as application:
            edges = application.graph_repository.load_all_edges_with_evidence()
            questions = discover_cross_document_questions(edges, count=args.count)
            records = []
            for index, question in enumerate(questions, start=1):
                print(f"Searching cross-document question {index}/{len(questions)}")
                result, _ = application.search_and_write(
                    question.question,
                    config_path=args.config,
                    run_id=f"{args.run_id}-{question.question_id}",
                    overwrite=args.overwrite,
                )
                records.append(evaluate_cross_document_result(question, result))
        manifest = write_cross_document_qa_artifacts(
            records,
            output_dir=search_config.artifact_root / args.run_id,
            config_path=args.config,
            embedding_model=application.embedding_client.model,
            embedding_dimension=application.embedding_client.dimension,
            overwrite=args.overwrite,
        )
        print(
            f"Completed cross-document QA {args.run_id}; "
            f"questions={manifest['question_count']}; "
            f"verified={manifest['expected_path_found_count']}; "
            f"passed={manifest['passed']}"
        )
        return 0 if manifest["passed"] else 1

    if args.command == "document-ingest":
        ingest_config = load_document_ingest_config(args.config)
        if ingest_config.env_file:
            _load_env_file(ingest_config.env_file)
        llm_config = OpenAICompatConfig.from_env()
        if args.allow_remote_llm:
            llm_config = replace(llm_config, local_only=False)
        client = OpenAICompatClient(llm_config)
        embedding_client = None
        embedding_batch_size = 64
        try:
            if ingest_config.build_load_files:
                embedding_config = EmbeddingConfig.from_env()
                if args.allow_remote_embedding:
                    embedding_config = replace(embedding_config, local_only=False)
                embedding_client = OpenAICompatEmbeddingClient(embedding_config)
                embedding_batch_size = embedding_config.batch_size
            manifest = run_document_ingest(
                ingest_config,
                client,
                embedding_client=embedding_client,
                embedding_batch_size=embedding_batch_size,
                overwrite=args.overwrite,
            )
        finally:
            client.close()
            if embedding_client is not None:
                embedding_client.close()
        print(
            f"Completed document ingest {manifest['run_id']}; "
            f"counts={json.dumps(manifest['counts'], ensure_ascii=False)}"
        )
        return 0

    if args.command == "segment":
        count = write_jsonl(args.output, (unit.to_dict() for unit in segment_paths(args.input)), overwrite=args.overwrite)
        manifest_count = 0
        if args.source_manifest_output:
            manifest_count = write_jsonl(
                args.source_manifest_output,
                build_source_manifest(args.input),
                overwrite=args.overwrite,
                leading_keys=("source_document_key", "document_id", "document_title"),
            )
        print(
            f"Wrote {count} semantic units to {args.output}; "
            f"{manifest_count} source manifest records"
        )
        return 0

    if args.command == "resolve-context":
        if args.env_file:
            _load_env_file(args.env_file)
        units = [SemanticUnit.from_dict(record) for record in read_jsonl(args.units)]
        if args.offset < 0:
            raise ValueError("--offset must be zero or greater")
        units = units[args.offset :]
        if args.limit is not None:
            if args.limit < 1:
                raise ValueError("--limit must be at least 1")
            units = units[: args.limit]
        if args.batch_size < 1:
            raise ValueError("--batch-size must be at least 1")
        completed_ids: set[str] = set()
        if args.resume and args.output.exists():
            completed_ids = {
                record["semantic_unit_id"] for record in read_jsonl(args.output)
            }
            units = [
                unit for unit in units if unit.semantic_unit_id not in completed_ids
            ]
        workers = args.workers or int(os.environ.get("VLLM_CONCURRENCY", "4"))
        config = OpenAICompatConfig.from_env()
        if args.allow_remote_llm:
            config = replace(config, local_only=False)
        client = OpenAICompatClient(config)
        errors_output = args.errors_output or args.output.with_suffix(".errors.jsonl")
        if not args.resume:
            write_jsonl(args.output, [], overwrite=args.overwrite)
            write_jsonl(errors_output, [], overwrite=args.overwrite)
        elif not args.output.exists():
            write_jsonl(args.output, [])
            write_jsonl(errors_output, [], overwrite=errors_output.exists())
        total_records = 0
        total_errors = 0
        prompt_template = (
            args.prompt_file.read_text(encoding="utf-8")
            if args.prompt_file
            else DEFAULT_CONTEXT_RESOLUTION_PROMPT
        )
        try:
            for start in range(0, len(units), args.batch_size):
                batch = units[start : start + args.batch_size]
                records, errors = resolve_context_units(
                    batch,
                    client,
                    workers=workers,
                    prompt_template=prompt_template,
                )
                append_jsonl(args.output, records)
                append_jsonl(errors_output, errors)
                total_records += len(records)
                total_errors += len(errors)
                print(
                    f"Progress {min(start + len(batch), len(units))}/{len(units)}: "
                    f"{total_records} records, {total_errors} failures",
                    flush=True,
                )
        finally:
            client.close()
        print(
            f"Wrote {total_records} context-resolved semantic units to {args.output}; "
            f"{total_errors} failures to {errors_output}; "
            f"skipped {len(completed_ids)} completed units"
        )
        return 0

    if args.command == "audit-context":
        if args.offset < 0:
            raise ValueError("--offset must be zero or greater")
        source_records = list(read_jsonl(args.source_units))[args.offset :]
        if args.limit is not None:
            if args.limit < 1:
                raise ValueError("--limit must be at least 1")
            source_records = source_records[: args.limit]
        report = audit_context_resolution(
            source_records,
            list(read_jsonl(args.resolved_units)),
        )
        if args.output.exists() and not args.overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {args.output}")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(
            f"Wrote context resolution audit to {args.output}; "
            f"passed={report['passed']}"
        )
        return 0 if report["passed"] else 1

    if args.command == "normalize-context":
        records = normalize_context_resolution_records(list(read_jsonl(args.input)))
        write_jsonl(args.output, records, overwrite=args.overwrite)
        print(f"Wrote {len(records)} normalized context records to {args.output}")
        return 0

    if args.command == "extract":
        if args.env_file:
            _load_env_file(args.env_file)
        units = [SemanticUnit.from_dict(record) for record in read_jsonl(args.units)]
        if args.offset < 0:
            raise ValueError("--offset must be zero or greater")
        units = units[args.offset :]
        if args.limit is not None:
            if args.limit < 1:
                raise ValueError("--limit must be at least 1")
            units = units[: args.limit]
        if args.batch_size < 1:
            raise ValueError("--batch-size must be at least 1")
        completed_ids: set[str] = set()
        if args.resume and args.output.exists():
            completed_ids = {record["semantic_unit_id"] for record in read_jsonl(args.output)}
            units = [unit for unit in units if unit.semantic_unit_id not in completed_ids]
        workers = args.workers or int(os.environ.get("VLLM_CONCURRENCY", "4"))
        config = OpenAICompatConfig.from_env()
        if args.allow_remote_llm:
            config = replace(config, local_only=False)
        client = OpenAICompatClient(config)
        errors_output = args.errors_output or args.output.with_suffix(".errors.jsonl")
        if not args.resume:
            write_jsonl(args.output, [], overwrite=args.overwrite)
            write_jsonl(errors_output, [], overwrite=args.overwrite)
        elif not args.output.exists():
            write_jsonl(args.output, [])
            write_jsonl(errors_output, [], overwrite=errors_output.exists())
        total_records = 0
        total_errors = 0
        try:
            prompt_template = load_prompt(args.prompt_file)
            for start in range(0, len(units), args.batch_size):
                batch = units[start : start + args.batch_size]
                records, errors = extract_units(
                    batch,
                    client,
                    workers=workers,
                    prompt_template=prompt_template,
                    preserve_llm_output=args.preserve_llm_output,
                )
                append_jsonl(args.output, records)
                append_jsonl(errors_output, errors)
                total_records += len(records)
                total_errors += len(errors)
                print(
                    f"Progress {min(start + len(batch), len(units))}/{len(units)}: "
                    f"{total_records} records, {total_errors} failures",
                    flush=True,
                )
        finally:
            client.close()
        print(
            f"Wrote {total_records} raw SVO records to {args.output}; "
            f"{total_errors} failures to {errors_output}; skipped {len(completed_ids)} completed units"
        )
        return 0

    if args.command == "audit-svo":
        report = audit_raw_svo(
            list(read_jsonl(args.units)),
            list(read_jsonl(args.raw_svo)),
            sample_limit=args.sample_limit,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if args.overwrite else "x"
        with args.output.open(mode, encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(f"Wrote standalone SVO audit to {args.output}; passed={report['passed']}")
        return 0

    if args.command == "audit-dictionaries":
        report = audit_dictionary_release(
            list(read_jsonl(args.raw_svo)),
            list(read_jsonl(args.entities)),
            list(read_jsonl(args.relations)),
            expected_relation_type_count=args.expected_relation_types,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if args.overwrite else "x"
        with args.output.open(mode, encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(
            f"Wrote dictionary audit to {args.output}; "
            f"passed={report['passed']}"
        )
        return 0 if report["passed"] else 1

    if args.command == "sanitize-svo":
        records = sanitize_raw_svo_records(list(read_jsonl(args.input)))
        if args.units:
            unit_ids = [record["semantic_unit_id"] for record in read_jsonl(args.units)]
            records = align_raw_svo_records(unit_ids, records)
        count = write_jsonl(args.output, records, overwrite=args.overwrite)
        print(f"Wrote {count} sanitized raw SVO records to {args.output}")
        return 0

    if args.command == "align-svo":
        unit_ids = [record["semantic_unit_id"] for record in read_jsonl(args.units)]
        records = [
            record
            for input_path in args.inputs
            for record in read_jsonl(input_path)
        ]
        aligned = align_raw_svo_records(unit_ids, records)
        count = write_jsonl(args.output, aligned, overwrite=args.overwrite)
        print(
            f"Aligned {count} raw SVO records from {len(args.inputs)} files "
            f"to {args.output}"
        )
        return 0

    if args.command == "seed-entity-map":
        mapping = build_seeded_entity_mapping(
            list(read_jsonl(args.entities_input)),
            list(read_jsonl(args.seed_dictionary)),
        )
        records = entity_mapping_records(mapping)
        count = write_jsonl(args.map_output, records, overwrite=args.overwrite)
        fallback_count = sum(
            record["normalization_status"].startswith("fallback")
            for record in records
        )
        print(
            f"Wrote {count} seeded entity mappings to {args.map_output}; "
            f"{fallback_count} require LLM normalization"
        )
        return 0

    if args.command == "refresh-relations":
        raw_records = list(read_jsonl(args.raw_svo))
        seed_dictionary = list(read_jsonl(args.seed_dictionary))
        relation_index = build_relation_alias_index(seed_dictionary)
        relation_by_id = {
            record["relation_type_id"]: record for record in seed_dictionary
        }
        overrides: dict[str, str] = {}
        if args.map_input:
            for record in read_jsonl(args.map_input):
                raw_predicate = str(record.get("raw_predicate", "")).strip()
                relation_type_id = str(record.get("relation_type_id", "")).strip()
                if raw_predicate and relation_type_id in relation_by_id:
                    overrides[raw_predicate] = relation_type_id
        observed_predicates = {
            relation["predicate"].strip()
            for record in raw_records
            for relation in record.get("relations", [])
            if relation.get("predicate", "").strip()
        }
        unmapped = sorted(
            observed_predicates - set(relation_index) - set(overrides)
        )
        errors: list[dict[str, object]] = []
        if unmapped:
            if args.env_file:
                _load_env_file(args.env_file)
            config = OpenAICompatConfig.from_env()
            if args.allow_remote_llm:
                config = replace(config, local_only=False)
            config = replace(config, max_tokens=args.max_tokens)
            client = OpenAICompatClient(config)
            try:
                proposed, errors = propose_forced_relation_overrides(
                    unmapped,
                    seed_dictionary,
                    client,
                    batch_size=args.batch_size,
                    workers=args.workers,
                )
            finally:
                client.close()
            overrides.update(proposed)
        mapping_records = [
            {
                "raw_predicate": predicate,
                "relation_type_id": relation_type_id,
                "canonical_name": relation_by_id[relation_type_id]["canonical_name"],
            }
            for predicate, relation_type_id in sorted(overrides.items())
        ]
        write_jsonl(args.map_output, mapping_records, overwrite=args.overwrite)
        write_jsonl(args.errors_output, errors, overwrite=args.overwrite)
        if errors:
            print(
                f"Mapped {len(overrides)} unseen predicates; "
                f"{len(errors)} batches require retry"
            )
            return 1
        dictionary = build_refreshed_relation_dictionary(
            raw_records,
            seed_dictionary,
            relation_overrides=overrides,
        )
        write_jsonl(
            args.dictionary_output,
            dictionary,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
        )
        print(
            f"Mapped all {len(observed_predicates)} observed predicates into "
            f"{len(dictionary)} closed relation types; "
            f"{len(overrides)} new aliases"
        )
        return 0

    if args.command == "cluster-entities":
        entity_records = list(read_jsonl(args.entities_input))
        if args.offset < 0:
            raise ValueError("--offset must be zero or greater")
        entity_records = entity_records[args.offset :]
        if args.limit is not None:
            if args.limit < 1:
                raise ValueError("--limit must be at least 1")
            entity_records = entity_records[: args.limit]
        errors: list[dict[str, object]] = []
        if args.map_input:
            mapping = entity_mapping_from_records(read_jsonl(args.map_input))
            if args.retry_fallback:
                fallback_names = {
                    alias
                    for alias, value in mapping.items()
                    if value["normalization_status"].startswith("fallback")
                }
                retry_records = [
                    record for record in entity_records if record["canonical_name"] in fallback_names
                ]
                if args.env_file:
                    _load_env_file(args.env_file)
                config = OpenAICompatConfig.from_env()
                if args.allow_remote_llm:
                    config = replace(config, local_only=False)
                client = OpenAICompatClient(config)
                try:
                    retry_mapping, errors = propose_entity_mapping(
                        retry_records,
                        client,
                        batch_size=args.batch_size,
                        workers=args.workers,
                    )
                finally:
                    client.close()
                mapping.update(retry_mapping)
        else:
            if args.env_file:
                _load_env_file(args.env_file)
            config = OpenAICompatConfig.from_env()
            if args.allow_remote_llm:
                config = replace(config, local_only=False)
            client = OpenAICompatClient(config)
            try:
                mapping, errors = propose_entity_mapping(
                    entity_records,
                    client,
                    batch_size=args.batch_size,
                    workers=args.workers,
                )
            finally:
                client.close()
        dictionary = build_clustered_entity_dictionary(
            entity_records,
            mapping,
            sample_limit=args.sample_limit,
        )
        write_jsonl(args.map_output, entity_mapping_records(mapping), overwrite=args.overwrite)
        write_jsonl(
            args.dictionary_output,
            dictionary,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
        )
        write_jsonl(args.errors_output, errors, overwrite=args.overwrite)
        print(
            f"Wrote {len(dictionary)} clustered entities and {len(mapping)} alias mappings; "
            f"{len(errors)} batch failures"
        )
        return 0

    if args.command == "compress-relations":
        relation_records = list(read_jsonl(args.relations_input))
        if args.env_file:
            _load_env_file(args.env_file)
        if args.taxonomy_input:
            taxonomy = list(read_jsonl(args.taxonomy_input))
        else:
            config = OpenAICompatConfig.from_env()
            if args.allow_remote_llm:
                config = replace(config, local_only=False)
            config = replace(config, max_tokens=args.max_tokens)
            client = OpenAICompatClient(config)
            try:
                taxonomy = propose_relation_taxonomy(
                    relation_records,
                    client,
                    target_families=args.target_families,
                    inventory_batch_size=args.taxonomy_batch_size,
                    workers=args.taxonomy_workers,
                )
            finally:
                client.close()
        write_jsonl(args.taxonomy_output, taxonomy, overwrite=args.overwrite)

        if args.map_input:
            mapping_records = list(read_jsonl(args.map_input))
            errors = []
        else:
            config = OpenAICompatConfig.from_env()
            if args.allow_remote_llm:
                config = replace(config, local_only=False)
            config = replace(config, max_tokens=args.max_tokens)
            client = OpenAICompatClient(config)
            try:
                mapping_records, errors = propose_closed_relation_mapping(
                    relation_records,
                    taxonomy,
                    client,
                    batch_size=args.batch_size,
                    workers=args.workers,
                )
            finally:
                client.close()
        mapping_records = sanitize_closed_relation_mapping(mapping_records, taxonomy)
        write_jsonl(args.map_output, mapping_records, overwrite=args.overwrite)
        write_jsonl(args.errors_output, errors, overwrite=args.overwrite)

        dictionary = build_closed_relation_dictionary(relation_records, mapping_records)
        write_jsonl(
            args.dictionary_output,
            dictionary,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
        )
        audit = audit_closed_relation_dictionary(
            relation_records,
            mapping_records,
            dictionary,
        )
        if args.audit_output.exists() and not args.overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {args.audit_output}")
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_output.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"Mapped {len(mapping_records)}/{len(relation_records)} source relation types "
            f"into {len(dictionary)} closed types; errors={len(errors)}; "
            f"audit_passed={audit['passed']}"
        )
        return 0

    if args.command == "renormalize-entities":
        entity_records = list(read_jsonl(args.entities_input))
        raw_records = list(read_jsonl(args.raw_svo))
        if args.require_all_llm and args.candidates_only:
            raise ValueError("--require-all-llm cannot be combined with --candidates-only")
        prompt_path = (
            args.prompt_file
            if args.prompt_file is not None
            else DEFAULT_PROMPT_ROOT / "entity_contextual_normalize.ko.md"
        ).resolve()
        prompt_template = prompt_path.read_text(encoding="utf-8").strip()
        prompt_suffix_path = (
            args.prompt_suffix_file.resolve()
            if args.prompt_suffix_file is not None
            else None
        )
        if prompt_suffix_path is not None:
            prompt_template = (
                f"{prompt_template}\n\n"
                f"{prompt_suffix_path.read_text(encoding='utf-8').strip()}"
            )
        prompt_metadata = {
            "path": str(prompt_path),
            "supplemental_path": (
                str(prompt_suffix_path) if prompt_suffix_path is not None else None
            ),
            "sha256": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest(),
        }
        llm_model: str | None = None
        if args.map_input:
            mapping_records = list(read_jsonl(args.map_input))
            errors = []
            if not args.require_all_llm:
                mapping_records = sanitize_contextual_entity_mapping(mapping_records)
            write_jsonl(args.map_output, mapping_records, overwrite=args.overwrite)
            write_jsonl(args.errors_output, errors, overwrite=args.overwrite)
        else:
            existing_mapping_records = (
                list(read_jsonl(args.map_output))
                if args.resume and args.map_output.exists()
                else []
            )
            if args.seed_llm_map_input is not None:
                seeded = rekey_llm_mapping_by_source_name(
                    entity_records,
                    list(read_jsonl(args.seed_llm_map_input)),
                )
                existing_by_id = {
                    str(item["source_entity_id"]): item
                    for item in [*seeded, *existing_mapping_records]
                }
                existing_mapping_records = list(existing_by_id.values())
            rejected_proposals: dict[str, list[str]] = {}
            if args.require_all_llm:
                for item in existing_mapping_records:
                    if not is_acceptable_llm_canonical_mapping(item):
                        rejected_proposals.setdefault(
                            str(item.get("source_entity_id", "")), []
                        ).append(str(item.get("canonical_name", "")))
                existing_mapping_records = [
                    item
                    for item in existing_mapping_records
                    if str(item.get("normalization_status", "")).startswith("llm_")
                    and is_acceptable_llm_canonical_mapping(item)
                ]
            if args.retry_fallback:
                existing_mapping_records = [
                    item
                    for item in existing_mapping_records
                    if not item.get("normalization_status", "").startswith("fallback")
                ]
            if args.candidates_only:
                lexical_groups: dict[str, list[str]] = {}
                for item in entity_records:
                    lexical_groups.setdefault(
                        entity_lexical_key(item["canonical_name"]), []
                    ).append(item["entity_id"])
                lexical_candidate_ids = {
                    entity_id
                    for entity_ids in lexical_groups.values()
                    if len(entity_ids) > 1
                    for entity_id in entity_ids
                }
                existing_ids = {
                    item["source_entity_id"] for item in existing_mapping_records
                }
                for item in entity_records:
                    if item["entity_id"] in existing_ids:
                        continue
                    if (
                        needs_contextual_entity_normalization(item["canonical_name"])
                        or item["entity_id"] in lexical_candidate_ids
                    ):
                        continue
                    existing_mapping_records.append(
                        {
                            "source_entity_id": item["entity_id"],
                            "source_canonical_name": item["canonical_name"],
                            "canonical_name": item["canonical_name"],
                            "normalization_status": "identity_not_selected_for_contextual_pass",
                        }
                    )
            completed_ids = {
                item["source_entity_id"] for item in existing_mapping_records
            }
            pending_entity_records = [
                item
                for item in entity_records
                if item["entity_id"] not in completed_ids
            ]
            if args.map_output.exists() and not args.resume and not args.overwrite:
                raise FileExistsError(f"Refusing to overwrite existing file: {args.map_output}")
            write_jsonl(
                args.map_output,
                existing_mapping_records,
                overwrite=args.map_output.exists(),
            )
            write_jsonl(
                args.errors_output,
                [],
                overwrite=args.errors_output.exists(),
            )

            def persist_progress(batch_mappings, batch_errors):
                append_jsonl(args.map_output, batch_mappings)
                append_jsonl(args.errors_output, batch_errors)

            if args.env_file:
                _load_env_file(args.env_file)
            config = OpenAICompatConfig.from_env()
            if args.allow_remote_llm:
                config = replace(config, local_only=False)
            config = replace(config, max_tokens=args.max_tokens)
            llm_model = config.model
            client = OpenAICompatClient(config)
            try:
                mapping_records, errors = propose_contextual_entity_mapping(
                    pending_entity_records,
                    raw_records,
                    client,
                    batch_size=args.batch_size,
                    workers=args.workers,
                    sample_limit=args.sample_limit,
                    prompt_template=prompt_template,
                    require_all_llm=args.require_all_llm,
                    rejected_proposals=rejected_proposals,
                    progress_callback=persist_progress,
                )
            finally:
                client.close()
            mapping_records = sorted(
                [*existing_mapping_records, *mapping_records],
                key=lambda item: item["source_canonical_name"],
            )
            if not args.require_all_llm:
                mapping_records = sanitize_contextual_entity_mapping(mapping_records)
            write_jsonl(args.map_output, mapping_records, overwrite=True)
            write_jsonl(args.errors_output, errors, overwrite=True)

        mapped_ids = {str(item.get("source_entity_id", "")) for item in mapping_records}
        source_ids = {str(item["entity_id"]) for item in entity_records}
        all_llm = all(
            str(item.get("normalization_status", "")).startswith("llm_")
            for item in mapping_records
        )
        if args.require_all_llm and (errors or mapped_ids != source_ids or not all_llm):
            audit = audit_contextual_entity_dictionary(
                entity_records,
                mapping_records,
                [],
                require_all_llm=True,
            )
            audit["llm_errors"] = len(errors)
            audit["mode"] = "llm_all_strict"
            audit["llm_model"] = llm_model
            audit["prompt"] = prompt_metadata
            if args.audit_output.exists() and not (args.overwrite or args.resume):
                raise FileExistsError(f"Refusing to overwrite existing file: {args.audit_output}")
            args.audit_output.parent.mkdir(parents=True, exist_ok=True)
            args.audit_output.write_text(
                json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(
                f"LLM canonical-name mapping incomplete: "
                f"mapped={len(mapped_ids)}/{len(source_ids)}; errors={len(errors)}; "
                "no dictionary was released"
            )
            return 1
        dictionary = build_contextually_normalized_entity_dictionary(
            entity_records,
            mapping_records,
            canonical_overrides={} if args.require_all_llm else None,
        )
        audit = audit_contextual_entity_dictionary(
            entity_records,
            mapping_records,
            dictionary,
            require_all_llm=args.require_all_llm,
        )
        audit["llm_errors"] = len(errors)
        audit["mode"] = "llm_all_strict" if args.require_all_llm else "hybrid"
        audit["llm_model"] = llm_model
        audit["prompt"] = prompt_metadata
        if args.audit_output.exists() and not (args.overwrite or args.resume):
            raise FileExistsError(f"Refusing to overwrite existing file: {args.audit_output}")
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_output.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if args.require_all_llm and (not audit["passed"] or errors):
            print(
                f"LLM canonical-name audit failed: mapped={len(mapping_records)}; "
                f"errors={len(errors)}; no dictionary was released"
            )
            return 1
        write_jsonl(
            args.dictionary_output,
            dictionary,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
        )
        print(
            f"Mapped {len(mapping_records)} source entities into "
            f"{len(dictionary)} entities; errors={len(errors)}; audit_passed={audit['passed']}"
        )
        return 0 if audit["passed"] and not errors else 1

    if args.command == "classify-entity-types":
        entity_records = list(read_jsonl(args.entities_input))
        errors = []
        prompt_path = (
            args.prompt_file
            if args.prompt_file is not None
            else DEFAULT_PROMPT_ROOT / "entity_type_classify.ko.md"
        ).resolve()
        prompt_template = prompt_path.read_text(encoding="utf-8").strip()
        llm_model: str | None = None
        if args.rules_only:
            for output_path in (args.map_output, args.errors_output):
                if output_path.exists() and not args.overwrite:
                    raise FileExistsError(f"Refusing to overwrite existing file: {output_path}")
            mapping_records = build_entity_type_mapping(entity_records)
        else:
            current_entity_ids = {str(item["entity_id"]) for item in entity_records}
            existing_mapping_records = (
                list(read_jsonl(args.map_output))
                if args.resume and args.map_output.exists()
                else []
            )
            existing_mapping_records = [
                item
                for item in existing_mapping_records
                if str(item.get("assignment_method", "")) == "llm"
                and str(item.get("entity_id", "")) in current_entity_ids
            ]
            if args.map_output.exists() and not args.resume and not args.overwrite:
                raise FileExistsError(f"Refusing to overwrite existing file: {args.map_output}")
            write_jsonl(
                args.map_output,
                existing_mapping_records,
                overwrite=args.map_output.exists(),
            )
            write_jsonl(
                args.errors_output,
                [],
                overwrite=args.errors_output.exists(),
            )
            completed_ids = {str(item["entity_id"]) for item in existing_mapping_records}
            pending = [
                item for item in entity_records if str(item["entity_id"]) not in completed_ids
            ]

            def persist_type_progress(batch_mappings, batch_errors):
                append_jsonl(args.map_output, batch_mappings)
                append_jsonl(args.errors_output, batch_errors)

            if pending:
                if args.env_file:
                    _load_env_file(args.env_file)
                config = OpenAICompatConfig.from_env()
                if args.allow_remote_llm:
                    config = replace(config, local_only=False)
                config = replace(config, max_tokens=args.max_tokens)
                llm_model = config.model
                client = OpenAICompatClient(config)
                try:
                    llm_mapping, errors = propose_llm_entity_types(
                        pending,
                        client,
                        prompt_template=prompt_template,
                        batch_size=args.batch_size,
                        workers=args.workers,
                        progress_callback=persist_type_progress,
                    )
                finally:
                    client.close()
            else:
                llm_mapping = []
            mapping_by_id = {
                str(item["entity_id"]): item
                for item in [*existing_mapping_records, *llm_mapping]
            }
            mapping_records = sorted(
                mapping_by_id.values(),
                key=lambda item: (str(item.get("canonical_name", "")), str(item["entity_id"])),
            )
        write_jsonl(
            args.map_output,
            mapping_records,
            overwrite=args.map_output.exists(),
            leading_keys=("canonical_name", "entity_type"),
        )
        write_jsonl(args.errors_output, errors, overwrite=args.errors_output.exists())
        audit = audit_entity_types(
            entity_records,
            mapping_records,
            require_all_llm=not args.rules_only,
        )
        audit["llm_errors"] = len(errors)
        audit["mode"] = "rules_only" if args.rules_only else "llm_all_strict"
        audit["llm_model"] = llm_model
        audit["prompt"] = {
            "path": str(prompt_path),
            "sha256": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest(),
        }
        if args.audit_output.exists() and not (args.overwrite or args.resume):
            raise FileExistsError(f"Refusing to overwrite existing file: {args.audit_output}")
        args.audit_output.parent.mkdir(parents=True, exist_ok=True)
        args.audit_output.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if not audit["passed"] or errors:
            print(
                f"LLM entity-type mapping incomplete: "
                f"mapped={len(mapping_records)}/{len(entity_records)}; "
                f"errors={len(errors)}; no typed dictionary was released"
            )
            return 1
        typed_dictionary = apply_entity_type_mapping(
            entity_records,
            mapping_records,
        )
        write_jsonl(
            args.dictionary_output,
            typed_dictionary,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases", "entity_type"),
        )
        print(
            f"Assigned types to {len(typed_dictionary)} entities; "
            f"counts={json.dumps(audit['counts']['entity_types'], ensure_ascii=False)}; "
            f"llm_errors={len(errors)}; passed={audit['passed']}"
        )
        return 0 if audit["passed"] else 1

    if args.command == "build-load-files":
        relation_overrides = None
        if args.relation_map:
            relation_overrides = relation_overrides_from_records(
                list(read_jsonl(args.relation_map))
            )
        embedding_client = None
        embedding_batch_size = 64
        if not args.without_embeddings:
            if args.env_file:
                _load_env_file(args.env_file)
            embedding_config = EmbeddingConfig.from_env()
            if args.allow_remote_embedding:
                embedding_config = replace(embedding_config, local_only=False)
            embedding_client = OpenAICompatEmbeddingClient(embedding_config)
            embedding_batch_size = (
                args.embedding_batch_size or embedding_config.batch_size
            )
        try:
            manifest = build_storage_load_files(
                list(read_jsonl(args.raw_svo)),
                list(read_jsonl(args.entities)),
                list(read_jsonl(args.relations)),
                dictionary_version=args.dictionary_version,
                output_dir=args.output,
                age_graph_name=args.age_graph_name,
                relation_overrides=relation_overrides,
                embedding_client=embedding_client,
                embedding_batch_size=embedding_batch_size,
                overwrite=args.overwrite,
            )
        finally:
            if embedding_client is not None:
                embedding_client.close()
        print(
            f"Wrote load release to {args.output}; "
            f"counts={json.dumps(manifest['counts'], ensure_ascii=False)}; "
            f"passed={manifest['passed']}"
        )
        return 0 if manifest["passed"] else 1

    if args.command == "candidates":
        if args.simple_with_source:
            entity_candidates, relation_candidates = build_simple_surface_lists(
                read_jsonl(args.raw_svo)
            )
            entity_leading_keys = ("name", "source_text")
            relation_leading_keys = ("name", "source_text")
        else:
            entity_candidates, relation_candidates = build_candidate_dictionaries(
                read_jsonl(args.raw_svo), sample_limit=args.sample_limit
            )
            entity_leading_keys = ("canonical_name", "aliases")
            relation_leading_keys = ("canonical_name", "aliases")
        write_jsonl(
            args.entities_output,
            entity_candidates,
            overwrite=args.overwrite,
            leading_keys=entity_leading_keys,
        )
        write_jsonl(
            args.relations_output,
            relation_candidates,
            overwrite=args.overwrite,
            leading_keys=relation_leading_keys,
        )
        print(
            f"Wrote {len(entity_candidates)} entity candidates to {args.entities_output}; "
            f"{len(relation_candidates)} relation candidates to {args.relations_output}"
        )
        return 0
    if args.command == "normalize":
        raw_records = list(read_jsonl(args.raw_svo))
        if args.relation_map_input:
            relation_mapping = relation_mapping_from_records(
                read_jsonl(args.relation_map_input)
            )
            errors = []
            if args.retry_fallback:
                fallback_aliases = {
                    alias
                    for alias, value in relation_mapping.items()
                    if value["normalization_status"].startswith("fallback_raw")
                }
                retry_records = []
                for record in raw_records:
                    relations = [
                        relation
                        for relation in record.get("relations", [])
                        if relation.get("predicate") in fallback_aliases
                    ]
                    if relations:
                        retry_records.append({**record, "relations": relations})
                if args.env_file:
                    _load_env_file(args.env_file)
                config = OpenAICompatConfig.from_env()
                if args.allow_remote_llm:
                    config = replace(config, local_only=False)
                client = OpenAICompatClient(config)
                try:
                    retry_mapping, errors = propose_relation_mapping(
                        retry_records,
                        client,
                        batch_size=args.relation_batch_size,
                        workers=args.relation_workers,
                    )
                finally:
                    client.close()
                relation_mapping.update(retry_mapping)
        else:
            if args.env_file:
                _load_env_file(args.env_file)
            config = OpenAICompatConfig.from_env()
            if args.allow_remote_llm:
                config = replace(config, local_only=False)
            client = OpenAICompatClient(config)
            try:
                relation_mapping, errors = propose_relation_mapping(
                    raw_records,
                    client,
                    batch_size=args.relation_batch_size,
                    workers=args.relation_workers,
                )
            finally:
                client.close()
        entities, relations, edges = build_first_pass_normalization(
            raw_records,
            relation_mapping,
            sample_limit=args.sample_limit,
        )
        write_jsonl(args.relation_map_output, relation_mapping_records(relation_mapping), overwrite=args.overwrite)
        write_jsonl(args.errors_output, errors, overwrite=args.overwrite)
        write_jsonl(
            args.entities_output,
            entities,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
        )
        write_jsonl(
            args.relations_output,
            relations,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
        )
        write_jsonl(args.edges_output, edges, overwrite=args.overwrite)
        print(
            f"Wrote {len(entities)} entities, {len(relations)} relation types, "
            f"{len(edges)} normalized edges; {len(errors)} relation-map batch failures"
        )
        return 0
    if args.command == "merge-jsonl":
        seen: set[str] = set()

        def merged_records():
            for input_path in args.inputs:
                for record in read_jsonl(input_path):
                    if args.dedupe_key:
                        value = record.get(args.dedupe_key)
                        if value is None:
                            raise ValueError(
                                f"Record in {input_path} has no de-duplication field {args.dedupe_key!r}"
                            )
                        value = str(value)
                        if value in seen:
                            continue
                        seen.add(value)
                    yield record

        count = write_jsonl(args.output, merged_records(), overwrite=args.overwrite)
        print(f"Merged {count} records from {len(args.inputs)} files into {args.output}")
        return 0
    if args.command == "audit":
        if args.output.exists() and not args.overwrite:
            raise FileExistsError(f"Refusing to overwrite existing file: {args.output}")
        report = audit_artifacts(
            list(read_jsonl(args.units)),
            list(read_jsonl(args.raw_svo)),
            list(read_jsonl(args.entities)),
            list(read_jsonl(args.relations)),
            list(read_jsonl(args.edges)),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report["counts"], ensure_ascii=False))
        if not all(report["checks"].values()):
            print(json.dumps(report["checks"], ensure_ascii=False))
            return 1
        return 0
    raise AssertionError(f"Unknown command: {args.command}")


def _load_env_file(path: Path) -> None:
    """Backward-compatible wrapper for callers importing the old private helper."""

    load_env_file(path)


if __name__ == "__main__":
    raise SystemExit(main())
