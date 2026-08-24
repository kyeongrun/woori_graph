#!/usr/bin/env python3
"""Audit and manifest the completed closed relation dictionary stage."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


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
        "candidates", "surface_map", "surface_dictionary", "taxonomy",
        "closed_map", "relations", "alias_map", "closed_audit",
        "audit_output", "manifest_output",
    ):
        parser.add_argument(f"--{name.replace('_', '-')}", dest=name, type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    candidates = read_jsonl(args.candidates)
    surface_map = read_jsonl(args.surface_map)
    source_types = read_jsonl(args.surface_dictionary)
    taxonomy = read_jsonl(args.taxonomy)
    closed_map = read_jsonl(args.closed_map)
    relations = read_jsonl(args.relations)
    aliases = read_jsonl(args.alias_map)
    closed_audit = json.loads(args.closed_audit.read_text(encoding="utf-8"))

    candidate_names = {item["name"] for item in candidates}
    relation_ids = {item["relation_type_id"] for item in relations}
    alias_names = [item["alias"] for item in aliases]
    checks = {
        "candidate_names_unique": len(candidate_names) == len(candidates),
        "surface_map_covers_all_raw_predicates": len(surface_map) == len(candidates)
        and {item["alias"] for item in surface_map} == candidate_names,
        "surface_map_has_no_fallback": all(
            not item.get("normalization_status", "").startswith("fallback")
            for item in surface_map
        ),
        "closed_audit_passed": closed_audit.get("passed") is True,
        "closed_map_covers_source_types": len(closed_map) == len(source_types),
        "relation_type_limit_met": len(relations) <= 100,
        "relation_type_ids_unique": len(relation_ids) == len(relations),
        "polarities_valid": all(item.get("polarity") in {"POSITIVE", "NEGATIVE"} for item in relations),
        "direct_aliases_unique": len(alias_names) == len(set(alias_names)),
        "raw_alias_coverage_exact": set(alias_names) == candidate_names,
        "alias_relation_references_valid": all(item.get("relation_type_id") in relation_ids for item in aliases),
        "mentions_preserved": sum(int(item.get("mention_count", 0)) for item in candidates)
        == sum(int(item.get("mention_count", 0)) for item in relations)
        == sum(int(item.get("mention_count", 0)) for item in aliases),
    }
    audit = {
        "passed": all(checks.values()),
        "counts": {
            "raw_relation_candidates": len(candidates),
            "source_relation_types": len(source_types),
            "taxonomy_families": len(taxonomy),
            "closed_relation_types": len(relations),
            "direct_aliases": len(aliases),
            "polarities": dict(sorted(Counter(item["polarity"] for item in relations).items())),
        },
        "checks": checks,
    }
    paths = {
        "candidates": args.candidates, "surface_map": args.surface_map,
        "surface_dictionary": args.surface_dictionary, "taxonomy": args.taxonomy,
        "closed_map": args.closed_map, "relations": args.relations,
        "alias_map": args.alias_map, "closed_audit": args.closed_audit,
    }
    git_result = subprocess.run(
        ["git", "-c", "safe.directory=D:/woori_graph", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    manifest = {
        "stage": "build-relation-map-and-dictionary",
        "status": "completed" if audit["passed"] else "failed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "program": {"git_parent_commit": git_result.stdout.strip() or None},
        "strategy": {
            "surface_normalization": "llm_positive_negative",
            "taxonomy": "fixed_50_family_seed_from_same_60_document_corpus",
            "maximum_relation_types": 100,
            "modality_types_created": False,
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
