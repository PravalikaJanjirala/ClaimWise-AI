"""Common helpers for verification agents."""
from __future__ import annotations
from ..schemas import Finding


class Agent:
    name = "agent"

    def run(self, ctx) -> list[Finding]:  # pragma: no cover - interface
        raise NotImplementedError


def get_field(ctx, doc_type, field_name):
    """Return (document_present, FieldValue or None)."""
    ex = ctx.extractions.get(doc_type)
    if ex is None:
        return False, None
    return True, ex.fields.get(field_name)
