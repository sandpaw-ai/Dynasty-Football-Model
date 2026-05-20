"""FantasyPros adapter — STUB.

FantasyPros offers an Expert Consensus Rankings (ECR) API on a paid tier:
    https://www.fantasypros.com/api/

This aggregates 70+ analysts into a consensus and is the cleanest legal way
to incorporate industry-wide expert opinion.

For the *free* path: DynastyProcess already publishes FantasyPros ECR via
their open CSV (see dynastyprocess.py). That's a reasonable substitute until
you have a direct FP key.
"""
from __future__ import annotations
from typing import Iterator
from .base import BaseSource, RankingRecord
from ..config import settings


class FantasyPros(BaseSource):
    slug = "fantasypros"
    name = "FantasyPros — Expert Consensus Rankings"
    category = "aggregator"
    update_frequency = "daily"
    tos_compliant = True
    default_weight = 1.2
    homepage = "https://www.fantasypros.com/"
    notes = "Paid API; stub only. See dynastyprocess.py for a free proxy."

    def fetch(self) -> Iterator[RankingRecord]:
        if not settings.fantasypros_api_key:
            return iter([])
        # TODO: call the FantasyPros ECR endpoint and yield RankingRecord per player
        return iter([])
