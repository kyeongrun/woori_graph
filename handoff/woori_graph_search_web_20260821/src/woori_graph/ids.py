"""Stable identifiers. They never depend on model response order."""

from __future__ import annotations

import uuid

_NAMESPACE = uuid.UUID("1170fe06-b0b0-4b95-8b7b-bb23f976da6f")


def stable_id(kind: str, *parts: object) -> str:
    return str(uuid.uuid5(_NAMESPACE, "|".join((kind, *(str(part) for part in parts)))))
