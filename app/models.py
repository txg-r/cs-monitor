from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    """统一生成带时区 UTC 时间，避免来源跨时区后出现排序歧义。"""

    return datetime.now(timezone.utc)


class Source(Base):
    """可轮询的信息源及其健康状态。"""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    url: Mapped[str] = mapped_column(Text)
    credibility: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, default=30)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    raw_items: Mapped[list[RawItem]] = relationship(back_populates="source")


class RawItem(Base):
    """来源内容的不可变快照，用于审计 AI 判断和重新分析。"""

    __tablename__ = "raw_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_raw_source_external"),
        Index("ix_raw_content_hash", "content_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(500))
    canonical_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str | None] = mapped_column(String(200), nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)

    source: Mapped[Source] = relationship(back_populates="raw_items")
    evidence: Mapped[EventEvidence | None] = relationship(back_populates="raw_item", uselist=False)
    analysis: Mapped[Analysis | None] = relationship(back_populates="raw_item", uselist=False)


class Event(Base):
    """将多个来源报道聚合后的市场事件。"""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    direction: Mapped[str] = mapped_column(String(20), index=True)
    impact_strength: Mapped[int] = mapped_column(Integer)
    time_horizon: Mapped[str] = mapped_column(String(30))
    confidence: Mapped[float] = mapped_column(Float)
    importance_score: Mapped[int] = mapped_column(Integer, index=True)
    alert_level: Mapped[str] = mapped_column(String(10), index=True)
    is_market_relevant: Mapped[bool] = mapped_column(Boolean, index=True)
    verified_facts: Mapped[list[str]] = mapped_column(JSON, default=list)
    entities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    affected_assets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    market_mechanisms: Mapped[list[str]] = mapped_column(JSON, default=list)
    uncertainties: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    evidence_count: Mapped[int] = mapped_column(Integer, default=1)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    evidence: Mapped[list[EventEvidence]] = relationship(back_populates="event", cascade="all, delete-orphan")
    notifications: Mapped[list[Notification]] = relationship(back_populates="event")


class EventEvidence(Base):
    """事件与原始证据的关联，保证页面能追溯每个判断的出处。"""

    __tablename__ = "event_evidence"
    __table_args__ = (UniqueConstraint("raw_item_id", name="uq_event_evidence_raw"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    event: Mapped[Event] = relationship(back_populates="evidence")
    raw_item: Mapped[RawItem] = relationship(back_populates="evidence")


class Analysis(Base):
    """单条证据的 AI 或规则分析结果。"""

    __tablename__ = "analyses"
    __table_args__ = (UniqueConstraint("raw_item_id", name="uq_analysis_raw"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    prompt_version: Mapped[str] = mapped_column(String(30))
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    raw_item: Mapped[RawItem] = relationship(back_populates="analysis")


class Notification(Base):
    """通知发件箱；唯一幂等键防止任务重试造成群内重复消息。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id", ondelete="SET NULL"), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    dedup_key: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    event: Mapped[Event | None] = relationship(back_populates="notifications")


class DailyReport(Base):
    """每日情报快照，保存生成时的内容以便历史回看。"""

    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_date: Mapped[str] = mapped_column(String(10), unique=True)
    content_markdown: Mapped[str] = mapped_column(Text)
    event_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class JobRun(Base):
    """记录后台任务结果，用于判断来源故障而不是静默漏报。"""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_name: Mapped[str] = mapped_column(String(100), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
