"""rookie_similarity_chain — moved to prospect-tool-only in v1.0.

The v0.16 college->NFL chain was tied to the v0.x composite. v1.0 leaves
this module as a stub. A clean rookie engine is on the v1.1 roadmap;
see ``docs/CHANGELOG-model.md``.
"""
from __future__ import annotations
from typing import Iterator
from .base import BaseSource, RankingRecord


class RookieSimilarityChain(BaseSource):
    slug = "rookie_similarity_chain"
    name = "Rookie Similarity Chain (moved to prospects-only in v1.0)"

    def fetch(self) -> Iterator[RankingRecord]:
        return iter(())
