"""Base source adapter — every ranking source implements this interface.

Adapters return normalized `RankingRecord` objects. The sync layer takes care of
resolving each record to a canonical Player row and writing to the DB.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Iterator
import httpx
from ..config import settings


@dataclass
class RankingRecord:
    """Normalized ranking record produced by a source adapter."""
    source_slug: str
    sleeper_id: Optional[str] = None           # canonical ID — preferred
    mfl_id: Optional[str] = None
    full_name: str = ""                         # fallback for name matching
    position: Optional[str] = None
    overall_rank: Optional[int] = None
    position_rank: Optional[int] = None
    market_value: Optional[float] = None
    tier: Optional[int] = None
    trend_30d: Optional[float] = None
    league_format: str = "sf_ppr"               # sf_ppr | 1qb_ppr | sf_te_premium | …
    is_dynasty: bool = True
    is_rookie_only: bool = False
    captured_at: datetime = field(default_factory=datetime.utcnow)
    # Optional player enrichment — sync.py will set these on the resolved Player.
    nfl_team: Optional[str] = None
    draft_year: Optional[int] = None
    age: Optional[float] = None


class BaseSource(ABC):
    """Abstract adapter for a ranking source.

    Subclasses must set the class attributes and implement `fetch()`.
    """
    slug: str = ""
    name: str = ""
    category: str = "expert"          # market | expert | model | aggregator
    update_frequency: str = "daily"    # daily | weekly | event
    tos_compliant: bool = True
    default_weight: float = 1.0
    homepage: str = ""
    notes: str = ""

    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )

    @abstractmethod
    def fetch(self) -> Iterator[RankingRecord]:
        """Fetch current rankings from the source, yielding RankingRecords."""
        ...

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
