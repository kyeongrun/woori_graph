"""Command line entry points for the v3 dictionary-building stage."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .candidates import build_candidate_dictionaries
from .closed_relations import (
    audit_closed_relation_dictionary,
    build_closed_relation_dictionary,
    propose_closed_relation_mapping,
    propose_relation_taxonomy,
    sanitize_closed_relation_mapping,
)
from .contextual_entities import (
    audit_contextual_entity_dictionary,
    build_contextually_normalized_entity_dictionary,
    entity_lexical_key,
    needs_contextual_entity_normalization,
    propose_contextual_entity_mapping,
    sanitize_contextual_entity_mapping,
)
from .audit import audit_artifacts, audit_dictionary_release, audit_raw_svo
from .documents import segment_paths
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
from .extraction import (
    OpenAICompatClient,
    OpenAICompatConfig,
    extract_units,
    align_raw_svo_records,
    load_prompt,
    sanitize_raw_svo_records,
)
from .graph_build.relation_mapper import propose_forced_relation_overrides
from .graph_mapping import build_relation_alias_index
from .jsonl import append_jsonl, read_jsonl, write_jsonl
from .models import SemanticUnit
from .normalization import (
    build_first_pass_normalization,
    propose_relation_mapping,
    relation_mapping_from_records,
    relation_mapping_records,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="woori-graph", description="v3 SVO dictionary-building pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)

    segment = subparsers.add_parser("segment", help="split law Markdown into semantic-units JSONL")
    segment.add_argument("--input", type=Path, required=True, help="Markdown file or directory")
    segment.add_argument("--output", type=Path, required=True, help="semantic-units JSONL path")
    segment.add_argument("--overwrite", action="store_true")

    extract = subparsers.add_parser("extract", help="extract raw SVO records through the local LLM")
    extract.add_argument("--units", type=Path, required=True, help="semantic-units JSONL path")
    extract.add_argument("--output", type=Path, required=True, help="raw SVO JSONL path")
    extract.add_argument("--errors-output", type=Path, help="failed extraction JSONL path")
    extract.add_argument("--prompt-file", type=Path, help="optional raw SVO prompt override")
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
    renormalize_entities.add_argument("--dictionary-output", type=Path, required=True)
    renormalize_entities.add_argument("--audit-output", type=Path, required=True)
    renormalize_entities.add_argument("--errors-output", type=Path, required=True)
    renormalize_entities.add_argument("--batch-size", type=int, default=40)
    renormalize_entities.add_argument("--workers", type=int, default=4)
    renormalize_entities.add_argument("--sample-limit", type=int, default=2)
    renormalize_entities.add_argument("--max-tokens", type=int, default=4096)
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
    renormalize_entities.add_argument("--overwrite", action="store_true")

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
    if args.command == "segment":
        count = write_jsonl(args.output, (unit.to_dict() for unit in segment_paths(args.input)), overwrite=args.overwrite)
        print(f"Wrote {count} semantic units to {args.output}")
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
                records, errors = extract_units(batch, client, workers=workers, prompt_template=prompt_template)
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
        if args.map_input:
            mapping_records = list(read_jsonl(args.map_input))
            errors = []
        else:
            existing_mapping_records = (
                list(read_jsonl(args.map_output))
                if args.resume and args.map_output.exists()
                else []
            )
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
            client = OpenAICompatClient(config)
            try:
                mapping_records, errors = propose_contextual_entity_mapping(
                    pending_entity_records,
                    raw_records,
                    client,
                    batch_size=args.batch_size,
                    workers=args.workers,
                    sample_limit=args.sample_limit,
                    progress_callback=persist_progress,
                )
            finally:
                client.close()
            mapping_records = sorted(
                [*existing_mapping_records, *mapping_records],
                key=lambda item: item["source_canonical_name"],
            )
            mapping_records = sanitize_contextual_entity_mapping(mapping_records)
            write_jsonl(args.map_output, mapping_records, overwrite=True)
        if args.map_input:
            mapping_records = sanitize_contextual_entity_mapping(mapping_records)
            write_jsonl(args.map_output, mapping_records, overwrite=args.overwrite)
            write_jsonl(args.errors_output, errors, overwrite=args.overwrite)
        dictionary = build_contextually_normalized_entity_dictionary(
            entity_records,
            mapping_records,
        )
        write_jsonl(
            args.dictionary_output,
            dictionary,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
        )
        audit = audit_contextual_entity_dictionary(
            entity_records,
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
            f"Mapped {len(mapping_records)} source entities into "
            f"{len(dictionary)} entities; errors={len(errors)}; audit_passed={audit['passed']}"
        )
        return 0

    if args.command == "candidates":
        entity_candidates, relation_candidates = build_candidate_dictionaries(
            read_jsonl(args.raw_svo), sample_limit=args.sample_limit
        )
        write_jsonl(
            args.entities_output,
            entity_candidates,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
        )
        write_jsonl(
            args.relations_output,
            relation_candidates,
            overwrite=args.overwrite,
            leading_keys=("canonical_name", "aliases"),
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
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
