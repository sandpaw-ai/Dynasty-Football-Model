"""dynastyprocess adapter — stubbed in v1.0.

Removed from the ranking composite. Kept as a no-op import target so any
external code referencing this module still loads cleanly. See
``docs/CHANGELOG-model.md`` v1.0 for the rewrite rationale.
"""
from __future__ import annotations
from typing import Iterator
from .base import BaseSource, RankingRecord


class _Stub(BaseSource):
    slug = "dynastyprocess"
    name = "dynastyprocess (removed in v1.0)"

    def fetch(self) -> Iterator[RankingRecord]:
        return iter(())


# Preserve historical class names for any code that imports them directly.
DynastyProcessValues = _Stub
