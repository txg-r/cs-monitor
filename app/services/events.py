from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Event, EventEvidence, RawItem, Source
from app.schemas import EventMergeCandidate, EventMergeDecision, MarketAnalysis
from app.services.scoring import calculate_importance, determine_alert_level
from app.services.text import normalize_title

logger = logging.getLogger(__name__)

MAX_EVENT_GAP_DAYS = 7
MAX_EVENT_TOTAL_SPAN_DAYS = 10
MAX_MERGE_CANDIDATES = 12


class EventMerger(Protocol):
    """事件归并的最小协议。"""

    async def compare(
        self,
        candidate: EventMergeCandidate,
        item: RawItem,
        analysis: MarketAnalysis,
    ) -> EventMergeDecision: ...


@dataclass
class CandidateEnvelope:
    """把数据库事件和归并提示词所需摘要放在一起。"""

    event: Event
    candidate: EventMergeCandidate
    gap_seconds: float


def _entity_values(entities: dict[str, list[str]]) -> set[str]:
    return {value.strip().lower() for values in entities.values() for value in values if value.strip()}


def _as_utc(value: datetime) -> datetime:
    """统一把来自 SQLite 的朴素时间按 UTC 解释。"""

    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _initial_fingerprint(item: RawItem, analysis: MarketAnalysis) -> str:
    material = {
        "event_type": analysis.event_type,
        "merge_keys": sorted(key.strip().lower() for key in analysis.merge_keys if key.strip()),
        "title": normalize_title(item.title),
    }
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _candidate_from_event(event: Event) -> CandidateEnvelope | None:
    published_times = [_as_utc(link.raw_item.published_at) for link in event.evidence if link.raw_item]
    if not published_times:
        return None
    sample_titles = [link.raw_item.title for link in sorted(event.evidence, key=lambda link: _as_utc(link.raw_item.published_at))[:3]]
    candidate = EventMergeCandidate(
        event_id=event.id,
        title=event.title,
        summary=event.summary,
        event_type=event.event_type,
        direction=event.direction,
        impact_strength=event.impact_strength,
        earliest_published_at=min(published_times),
        latest_published_at=max(published_times),
        evidence_count=event.evidence_count,
        verified_facts=event.verified_facts or [],
        entities=event.entities or {},
        affected_assets=event.affected_assets or [],
        sample_titles=sample_titles,
    )
    return CandidateEnvelope(event=event, candidate=candidate, gap_seconds=0.0)


def _fits_time_window(candidate: EventMergeCandidate, item_published_at: datetime) -> tuple[bool, float]:
    """只保留发布时间靠近的新旧事件。

    这一步不是替代 AI，而是防止“同一标题的周更/季更”反复串连。只要时间跨度过大，
    即便模型看见很多共性，也不应该进入同一事件候选池。
    """

    earliest = _as_utc(candidate.earliest_published_at)
    latest = _as_utc(candidate.latest_published_at)
    item_published_at = _as_utc(item_published_at)

    if earliest <= item_published_at <= latest:
        gap = 0.0
    else:
        gap = min(abs((item_published_at - earliest).total_seconds()), abs((item_published_at - latest).total_seconds()))

    total_span = (max(latest, item_published_at) - min(earliest, item_published_at)).total_seconds()
    return (
        gap <= timedelta(days=MAX_EVENT_GAP_DAYS).total_seconds()
        and total_span <= timedelta(days=MAX_EVENT_TOTAL_SPAN_DAYS).total_seconds(),
        gap,
    )


async def find_related_event(
    db: Session,
    item: RawItem,
    analysis: MarketAnalysis,
    merger: EventMerger,
) -> Event | None:
    """使用 AI 判断候选事件是否与当前 raw 属于同一市场事件。"""

    recent_events = db.scalars(
        select(Event)
        .where(Event.is_market_relevant.is_(True))
        .options(selectinload(Event.evidence).selectinload(EventEvidence.raw_item))
        .order_by(Event.updated_at.desc())
        .limit(40)
    ).all()

    candidates: list[CandidateEnvelope] = []
    for event in recent_events:
        envelope = _candidate_from_event(event)
        if envelope is None:
            continue
        fits, gap_seconds = _fits_time_window(envelope.candidate, item.published_at)
        if not fits:
            continue
        envelope.gap_seconds = gap_seconds
        candidates.append(envelope)

    candidates.sort(key=lambda envelope: envelope.gap_seconds)
    logger.debug(
            "开始扫描归并候选：原始ID=%s，标题=%s，候选数量=%s，归并锚点=%s",
        item.id,
        item.title[:120],
        len(candidates),
        ",".join(analysis.merge_keys),
    )
    for envelope in candidates[:MAX_MERGE_CANDIDATES]:
        decision = await merger.compare(envelope.candidate, item, analysis)
        logger.debug(
            "归并判定结果：原始ID=%s，候选事件ID=%s，是否同一事件=%s，置信度=%.2f，时间间隔秒数=%.0f，原因=%s",
            item.id,
            envelope.event.id,
            decision.same_event,
            decision.confidence,
            envelope.gap_seconds,
            decision.reason[:240],
        )
        if decision.same_event and decision.confidence >= 0.75:
            logger.info(
            "原始内容匹配到已有事件：原始ID=%s，事件ID=%s，置信度=%.2f，事件标题=%s",
                item.id,
                envelope.event.id,
                decision.confidence,
                envelope.event.title[:160],
            )
            return envelope.event
    return None


async def upsert_event(
    db: Session,
    source: Source,
    item: RawItem,
    analysis: MarketAnalysis,
    merger: EventMerger,
) -> tuple[Event, bool, bool]:
    """创建或合并事件。"""

    related = await find_related_event(db, item, analysis, merger)
    if related is None:
        score = calculate_importance(source, item, analysis, evidence_count=1, is_new_event=True)
        level = determine_alert_level(source, item, analysis, score, 1)
        event = Event(
            fingerprint=_initial_fingerprint(item, analysis),
            title=item.title,
            summary=analysis.summary_zh,
            event_type=analysis.event_type,
            direction=analysis.direction,
            impact_strength=analysis.impact_strength,
            time_horizon=analysis.time_horizon,
            confidence=analysis.confidence,
            importance_score=score,
            alert_level=level,
            is_market_relevant=analysis.is_market_relevant,
            verified_facts=analysis.verified_facts,
            entities=analysis.entities,
            affected_assets=[asset.model_dump() for asset in analysis.affected_assets],
            market_mechanisms=analysis.market_mechanisms,
            uncertainties=analysis.uncertainties,
        )
        db.add(event)
        db.flush()
        db.add(EventEvidence(event_id=event.id, raw_item_id=item.id))
        logger.info(
            "创建新事件：事件ID=%s，原始ID=%s，来源=%s，事件类型=%s，评级=%s，评分=%s，标题=%s",
            event.id,
            item.id,
            source.name,
            analysis.event_type,
            level,
            score,
            item.title[:160],
        )
        return event, True, False

    previous_confidence = related.confidence
    old_direction = related.direction
    old_strength = related.impact_strength

    related.evidence_count += 1
    related.confidence = max(related.confidence, analysis.confidence)
    related.importance_score = max(
        related.importance_score,
        calculate_importance(source, item, analysis, evidence_count=related.evidence_count, is_new_event=False),
    )
    if analysis.confidence >= previous_confidence or analysis.impact_strength > related.impact_strength:
        related.title = item.title if source.credibility >= 90 else related.title
        related.summary = analysis.summary_zh
        related.direction = analysis.direction
        related.impact_strength = analysis.impact_strength
        related.time_horizon = analysis.time_horizon
    related.alert_level = determine_alert_level(
        source, item, analysis, related.importance_score, related.evidence_count
    )
    related.verified_facts = list(dict.fromkeys((related.verified_facts or []) + analysis.verified_facts))[:20]
    related.uncertainties = list(dict.fromkeys((related.uncertainties or []) + analysis.uncertainties))[:20]
    merged_entities = dict(related.entities or {})
    for key, values in (analysis.entities or {}).items():
        existing_values = merged_entities.get(key, [])
        merged_entities[key] = list(dict.fromkeys(existing_values + values))
    related.entities = merged_entities
    if analysis.market_mechanisms:
        related.market_mechanisms = list(dict.fromkeys((related.market_mechanisms or []) + analysis.market_mechanisms))[:12]
    if analysis.affected_assets:
        related_assets = list(related.affected_assets or [])
        existing_assets = {(asset.get("category"), tuple(asset.get("names", []))) for asset in related_assets}
        for asset in analysis.affected_assets:
            key = (asset.category, tuple(asset.names))
            if key not in existing_assets:
                related_assets.append(asset.model_dump())
                existing_assets.add(key)
        related.affected_assets = related_assets
    db.add(EventEvidence(event_id=related.id, raw_item_id=item.id))
    significant_update = old_direction != related.direction or abs(old_strength - related.impact_strength) >= 2
    logger.info(
            "合并到已有事件：事件ID=%s，原始ID=%s，证据数量=%s，评级=%s，评分=%s，是否重大更新=%s",
        related.id,
        item.id,
        related.evidence_count,
        related.alert_level,
        related.importance_score,
        significant_update,
    )
    return related, False, significant_update
