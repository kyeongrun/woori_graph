#!/usr/bin/env python3
"""Verify the committed context-complete dictionary-build artifact.

This script intentionally uses only the Python standard library so a reviewer can
run it immediately after cloning the repository, before installing the project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterator


DEFAULT_RELEASE = Path(
    "artifacts/dictionary-build/v4-context-complete-20260821"
)
REQUIRED_UNIT_FIELDS = {
    "document_id",
    "document_title",
    "semantic_unit_id",
    "source_path",
    "source_ref",
    "unit_text",
    "governing_text",
    "resolved_text",
    "resolution_type",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object expected: {path}")
    return value


def read_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSON object expected: {path}:{line_number}")
            yield line_number, value


def verify_release(release_root: Path) -> dict[str, Any]:
    manifest_path = release_root / "work" / "01_segmentation_manifest.json"
    audit_path = release_root / "work" / "audits" / "01_context_resolution_audit.json"
    source_manifest_path = release_root / "source_manifest.jsonl"
    units_path = release_root / "work" / "01_semantic_units.jsonl"

    required_paths = (
        manifest_path,
        audit_path,
        source_manifest_path,
        units_path,
    )
    missing = [str(path) for path in required_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Missing release files: " + ", ".join(missing))

    manifest = read_json(manifest_path)
    audit = read_json(audit_path)
    artifact_manifest = manifest.get("artifacts", {})

    expected_files = {
        "source_manifest": source_manifest_path,
        "semantic_units": units_path,
        "audit": audit_path,
    }
    hash_results: dict[str, str] = {}
    for name, path in expected_files.items():
        expected_hash = artifact_manifest.get(name, {}).get("sha256")
        actual_hash = sha256(path)
        if actual_hash != expected_hash:
            raise ValueError(
                f"SHA-256 mismatch for {name}: expected {expected_hash}, got {actual_hash}"
            )
        hash_results[name] = actual_hash

    document_ids: set[str] = set()
    source_paths: set[str] = set()
    source_count = 0
    for line_number, record in read_jsonl(source_manifest_path):
        source_count += 1
        document_id = record.get("document_id")
        source_path = record.get("source_path")
        if not document_id or not source_path:
            raise ValueError(
                f"Missing document_id/source_path: {source_manifest_path}:{line_number}"
            )
        if document_id in document_ids:
            raise ValueError(f"Duplicate document_id: {document_id}")
        if source_path in source_paths:
            raise ValueError(f"Duplicate source_path: {source_path}")
        document_ids.add(document_id)
        source_paths.add(source_path)

    unit_ids: set[str] = set()
    resolution_types: Counter[str] = Counter()
    unit_count = 0
    for line_number, record in read_jsonl(units_path):
        unit_count += 1
        missing_fields = REQUIRED_UNIT_FIELDS - record.keys()
        if missing_fields:
            raise ValueError(
                f"Missing fields {sorted(missing_fields)}: {units_path}:{line_number}"
            )
        semantic_unit_id = record["semantic_unit_id"]
        if semantic_unit_id in unit_ids:
            raise ValueError(f"Duplicate semantic_unit_id: {semantic_unit_id}")
        if not record["resolved_text"].strip():
            raise ValueError(f"Empty resolved_text: {units_path}:{line_number}")
        unit_ids.add(semantic_unit_id)
        resolution_types[record["resolution_type"]] += 1

    expected_source_count = artifact_manifest["source_manifest"]["record_count"]
    expected_unit_count = artifact_manifest["semantic_units"]["record_count"]
    expected_resolution_types = {
        "COPIED": artifact_manifest["semantic_units"]["copied_count"],
        "CONTEXT_INHERITED": artifact_manifest["semantic_units"][
            "context_inherited_count"
        ],
    }
    if source_count != expected_source_count:
        raise ValueError(
            f"Source count mismatch: expected {expected_source_count}, got {source_count}"
        )
    if unit_count != expected_unit_count:
        raise ValueError(
            f"Unit count mismatch: expected {expected_unit_count}, got {unit_count}"
        )
    if dict(resolution_types) != expected_resolution_types:
        raise ValueError(
            "Resolution counts mismatch: "
            f"expected {expected_resolution_types}, got {dict(resolution_types)}"
        )
    if not audit.get("passed"):
        raise ValueError("Committed context-resolution audit did not pass")

    return {
        "passed": True,
        "release_root": str(release_root),
        "documents": source_count,
        "semantic_units": unit_count,
        "resolution_types": dict(resolution_types),
        "sha256": hash_results,
        "committed_audit_passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify hashes, JSONL structure, counts, and audit status of a release"
    )
    parser.add_argument(
        "--release-root",
        type=Path,
        default=DEFAULT_RELEASE,
        help=f"release directory (default: {DEFAULT_RELEASE.as_posix()})",
    )
    args = parser.parse_args()

    try:
        report = verify_release(args.release_root.resolve())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
