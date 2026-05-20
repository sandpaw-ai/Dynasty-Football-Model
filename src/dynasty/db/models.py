"""SQLAlchemy 2.0 ORM models — the core data schema.

Key design decisions:
 - `players` is the canonical entity; `sleeper_id` is the preferred external ID
   because Sleeper's player dictionary cross-references most other systems.
 - `rankings` is a *time-series* table. Every sync appends new rows rather than
   overwriting — this is what powers trend signals and backtesting.
 - `production` holds both weekly rows (week=1..18) and season totals (week=NULL).
 - `composite_scores` is the model's output, recomputed on each `score` run.
 - `source_track_record` stores backtested accuracy and is used to adjust source
   weights in the composite scoring step.
"""
from __future__ import annotations
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    String, Integer, Float, Boolean, DateTime, Date, ForeignKey, Text,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)

    # External IDs. sleeper_id is canonical.
    sleeper_id: Mapped[Optional[str]] = mapped_column(String(32), unique=True, index=True)
    mfl_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    fantasycalc_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    espn_id: Mapped[Optional[str]] = mapped_column(String(32))
    yahoo_id: Mapped[Optional[str]] = mapped_column(String(32))
    pfr_id: Mapped[Optional[str]] = mapped_column(String(32))  # pro-football-reference
    gsis_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)  # nflverse / NFL GSIS id, e.g. "00-0033280"

    full_name: Mapped[str] = mapped_column(String(128))
    # Suffix-stripped lowercase form for duplicate detection: "odell beckham"
    # matches "Odell Beckham", "Odell Beckham Jr.", and "Odell Beckham, Jr.".
    normalized_name: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(64))
    last_name: Mapped[Optional[str]] = mapped_column(String(64))
    position: Mapped[Optional[str]] = mapped_column(String(8))
    nfl_team: Mapped[Optional[str]] = mapped_column(String(8))

    birthdate: Mapped[Optional[date]] = mapped_column(Date)
    height_inches: Mapped[Optional[int]] = mapped_column(Integer)
    weight_lbs: Mapped[Optional[int]] = mapped_column(Integer)
    college: Mapped[Optional[str]] = mapped_column(String(128))

    draft_year: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    draft_round: Mapped[Optional[int]] = mapped_column(Integer)
    draft_pick_overall: Mapped[Optional[int]] = mapped_column(Integer)
    draft_team: Mapped[Optional[str]] = mapped_column(String(8))

    is_prospect: Mapped[bool] = mapped_column(Boolean, default=False)  # not yet drafted
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    rankings: Mapped[list["Ranking"]] = relationship(back_populates="player", cascade="all, delete-orphan")
    productions: Mapped[list["Production"]] = relationship(back_populates="player")
    evaluations: Mapped[list["Evaluation"]] = relationship(back_populates="player")

    __table_args__ = (
        Index("ix_players_name_pos", "full_name", "position"),
    )


class Source(Base):
    """A ranking source — FantasyCalc, PFF, Matt Harmon, etc."""
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32))  # market | expert | model | aggregator
    url: Mapped[Optional[str]] = mapped_column(String(256))
    update_frequency: Mapped[str] = mapped_column(String(16), default="daily")  # daily | weekly | event
    tos_compliant: Mapped[bool] = mapped_column(Boolean, default=True)

    # Starting weight in composite scoring (0..1+). Adjusted by track record at score time.
    default_weight: Mapped[float] = mapped_column(Float, default=1.0)

    notes: Mapped[Optional[str]] = mapped_column(Text)
    last_synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_sync_status: Mapped[Optional[str]] = mapped_column(String(32))  # ok | error
    last_sync_error: Mapped[Optional[str]] = mapped_column(Text)

    rankings: Mapped[list["Ranking"]] = relationship(back_populates="source")


class Ranking(Base):
    """Time-series ranking record. One row per (source, player, snapshot)."""
    __tablename__ = "rankings"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)

    overall_rank: Mapped[Optional[int]] = mapped_column(Integer)
    position_rank: Mapped[Optional[int]] = mapped_column(Integer)
    market_value: Mapped[Optional[float]] = mapped_column(Float)  # KTC-style 0..10000
    tier: Mapped[Optional[int]] = mapped_column(Integer)

    league_format: Mapped[str] = mapped_column(String(32), default="sf_ppr")
    is_dynasty: Mapped[bool] = mapped_column(Boolean, default=True)
    is_rookie_only: Mapped[bool] = mapped_column(Boolean, default=False)

    trend_30d: Mapped[Optional[float]] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    source: Mapped["Source"] = relationship(back_populates="rankings")
    player: Mapped["Player"] = relationship(back_populates="rankings")

    __table_args__ = (
        Index("ix_rankings_source_player_captured", "source_id", "player_id", "captured_at"),
        Index("ix_rankings_format_captured", "league_format", "captured_at"),
    )


class Production(Base):
    """Actual NFL fantasy production. week=NULL means season totals."""
    __tablename__ = "production"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    week: Mapped[Optional[int]] = mapped_column(Integer)

    nfl_team: Mapped[Optional[str]] = mapped_column(String(8))
    games_played: Mapped[Optional[int]] = mapped_column(Integer)

    fantasy_points_ppr: Mapped[Optional[float]] = mapped_column(Float)
    fantasy_points_half_ppr: Mapped[Optional[float]] = mapped_column(Float)
    fantasy_points_std: Mapped[Optional[float]] = mapped_column(Float)

    season_position_rank: Mapped[Optional[int]] = mapped_column(Integer)

    source: Mapped[str] = mapped_column(String(32), default="manual")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    player: Mapped["Player"] = relationship(back_populates="productions")

    __table_args__ = (
        UniqueConstraint("player_id", "season", "week", name="uix_production_player_season_week"),
    )


class Evaluation(Base):
    """Granular evaluations (PFF grade, Reception Perception route success, model scores).

    Use `metric` as a string discriminator so you can mix many score types in one table.
    """
    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)

    metric: Mapped[str] = mapped_column(String(64))         # e.g. 'pff_grade', 'rp_success_rate'
    value: Mapped[Optional[float]] = mapped_column(Float)
    max_value: Mapped[Optional[float]] = mapped_column(Float)  # e.g. 100 for PFF
    context: Mapped[Optional[str]] = mapped_column(String(128))  # 'season=2024', 'vs_man', etc.

    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    player: Mapped["Player"] = relationship(back_populates="evaluations")


class CompositeScore(Base):
    """Output of the model: blended dynasty score per player. Append-only history."""
    __tablename__ = "composite_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    league_format: Mapped[str] = mapped_column(String(32))

    score: Mapped[float] = mapped_column(Float)
    overall_rank: Mapped[int] = mapped_column(Integer)
    position_rank: Mapped[int] = mapped_column(Integer)
    tier: Mapped[Optional[int]] = mapped_column(Integer)

    # Consensus comparison fields (added in 0.2.0)
    consensus_rank: Mapped[Optional[int]] = mapped_column(Integer)
    rank_divergence: Mapped[Optional[int]] = mapped_column(Integer)  # consensus_rank - model_rank
    # positive = model likes more than consensus, negative = model lower than consensus

    breakdown_json: Mapped[Optional[str]] = mapped_column(Text)  # per-source contributions
    model_version: Mapped[str] = mapped_column(String(32), default="0.1.0")

    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class SourceTrackRecord(Base):
    """Backtested accuracy per source/position/cohort."""
    __tablename__ = "source_track_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    position: Mapped[Optional[str]] = mapped_column(String(8))   # NULL = overall
    cohort_year: Mapped[Optional[int]] = mapped_column(Integer)  # NULL = aggregated
    is_rookie_eval: Mapped[bool] = mapped_column(Boolean, default=True)

    outcome_window_years: Mapped[int] = mapped_column(Integer, default=3)
    sample_size: Mapped[int] = mapped_column(Integer)

    spearman_corr: Mapped[Optional[float]] = mapped_column(Float)
    r_squared: Mapped[Optional[float]] = mapped_column(Float)
    mae: Mapped[Optional[float]] = mapped_column(Float)
    hit_rate_top12: Mapped[Optional[float]] = mapped_column(Float)
    hit_rate_top24: Mapped[Optional[float]] = mapped_column(Float)

    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
