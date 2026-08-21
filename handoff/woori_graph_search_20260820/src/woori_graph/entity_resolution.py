"""Small deterministic exceptions to scope-free entity name handling."""

from __future__ import annotations


SELF_REFERENCES = {
    "이 법",
    "이 영",
    "이 규칙",
    "이 규정",
    "본 법",
    "본 영",
    "본 규칙",
    "본 규정",
}


def resolve_entity_name(raw_name: str, *, document_title: str) -> tuple[str, str]:
    """Resolve only unambiguous current-document self references.

    General names such as 위원회, 회사, 담당자 remain unchanged even when a
    formal name can be inferred elsewhere in the document.
    """

    name = raw_name.strip()
    if name in SELF_REFERENCES:
        return document_title.strip(), "current_document_self_reference"
    return name, "exact_surface"
