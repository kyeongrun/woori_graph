#!/usr/bin/env python3
"""Audit and manifest the completed two-pass entity normalization stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ENTITY_TYPES = {"ORGANIZATION", "PERSON", "LEGAL_INSTRUMENT", "CONCEPT", "OTHER"}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in (
        "candidates", "name_map", "contextual_map", "entities", "alias_map",
        "context_audit", "type_audit", "audit_output", "manifest_output",
    ):
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    candidates = read_jsonl(args.candidates)
    name_map = read_jsonl(args.name_map)
    contextual_map = read_jsonl(args.contextual_map)
    entities = read_jsonl(args.entities)
    alias_rows = read_jsonl(args.alias_map)
    context_audit = json.loads(args.context_audit.read_text(encoding="utf-8"))
    type_audit = json.loads(args.type_audit.read_text(encoding="utf-8"))

    candidate_names = {item["name"] for item in candidates}
    alias_names = [item["alias"] for item in alias_rows]
    entity_ids = {item["entity_id"] for item in entities}
    entity_types_by_id = {item["entity_id"]: item["entity_type"] for item in entities}
    checks = {
        "candidate_names_unique": len(candidate_names) == len(candidates),
        "first_pass_maps_all_candidates": len(name_map) == len(candidates)
        and {item["alias"] for item in name_map} == candidate_names,
        "contextual_map_covers_first_pass_entities": context_audit.get("checks", {}).get(
            "all_source_entities_mapped", False
        ),
        "contextual_audit_passed": context_audit.get("passed") is True,
        "type_audit_passed": type_audit.get("passed") is True,
        "entity_ids_unique": len(entity_ids) == len(entities),
        "entity_types_closed": all(item.get("entity_type") in ENTITY_TYPES for item in entities),
        "alias_names_unique": len(alias_names) == len(set(alias_names)),
        "raw_alias_coverage_exact": candidate_names <= set(alias_names),
        "alias_entity_references_valid": all(item.get("entity_id") in entity_ids for item in alias_rows),
        "alias_types_match_entities": all(
            item.get("entity_type") == entity_types_by_id.get(item.get("entity_id"))
            for item in alias_rows
        ),
        "mentions_preserved": sum(int(item.get("mention_count", 0)) for item in entities)
        == sum(int(item.get("mention_count", 0)) for item in candidates),
    }
    audit = {
        "passed": all(checks.values()),
        "counts": {
            "raw_entity_candidates": len(candidates),
            "first_pass_mappings": len(name_map),
            "contextual_mappings": len(contextual_map),
            "final_entities": len(entities),
            "direct_aliases": len(alias_rows),
            "entity_types": dict(sorted(Counter(item["entity_type"] for item in entities).items())),
        },
        "checks": checks,
    }
    paths = {
        "candidates": args.candidates,
        "name_map": args.name_map,
        "contextual_map": args.contextual_map,
        "entities": args.entities,
        "alias_map": args.alias_map,
        "context_audit": args.context_audit,
        "type_audit": args.type_audit,
    }
    git_result = subprocess.run(
        ["git", "-c", "safe.directory=D:/woori_graph", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    manifest = {
        "stage": "build-entity-map-and-dictionary",
        "status": "completed" if audit["passed"] else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "program": {"git_parent_commit": git_result.stdout.strip() or None},
        "strategy": {
            "first_pass": "llm_name_normalization",
            "second_pass": "llm_contextual_normalization_all_strict",
            "entity_types": sorted(ENTITY_TYPES),
            "legacy_overrides_applied": False,
        },
        "artifacts": {
            name: {"path": path.as_posix(), "sha256": sha256(path)}
            for name, path in paths.items()
        },
        "counts": audit["counts"],
        "audit_passed": audit["passed"],
    }
    mode = "w" if args.overwrite else "x"
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    with args.audit_output.open(mode, encoding="utf-8", newline="\n") as stream:
        json.dump(audit, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    with args.manifest_output.open(mode, encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return 0 if audit["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
