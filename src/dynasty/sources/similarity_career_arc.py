"""similarity_career_arc — replaced by ``dynasty.engine.similarity_v1`` in v1.0.

This module was the v0.14+ similarity engine that fed the v0.x composite. v1.0
folds its concerns into a single, cleaner ``dynasty.engine.similarity_v1``
module operating on a retired-only corpus with era-pace projection.

The stub here exists only so callers importing the old class don't crash.
``load_vorp_debug`` is preserved as a no-op for the CI log dump.
"""
from __future__ import annotations
from typing import Dict, Iterator
from .base import BaseSource, RankingRecord


class SimilarityCareerArc(BaseSource):
    slug = "similarity_career_arc"
    name = "Similarity Career Arc (removed in v1.0 — see engine.similarity_v1)"

    def fetch(self) -> Iterator[RankingRecord]:
        return iter(())


def load_vorp_debug() -> Dict[str, dict]:
    return {}
