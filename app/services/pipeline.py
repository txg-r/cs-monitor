import logging
from datetime import timedelta
from typing import Protocol

import httpx
from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.collectors.registry import get_collector
from app.config import Settings
from app.models import Analysis, DailyReport, Event, EventEvidence, Notification, RawItem, Source, utc_now
from app.schemas import CollectedItem, MarketAnalysis
from app.services.ai import (
    AIAnalysisError,
    NullEventMerger,
    OpenAICompatibleAnalyzer,
    OpenAICompatibleEventMerger,
    PROMPT_VERSION,
    RuleBasedAnalyzer,
)
from app.services.events import EventMerger, upsert_event
from app.services.relevance import is_candidate
from app.services.text import canonicalize_url, compact_whitespace, content_hash

logger = logging.getLogger(__name__)


def describe_exception(exc: Exception) -> str:
    """把异常稳定转换成可展示文案。

    某些底层网络异常的 `str(exc)` 可能是空串。
    如果直接落库，前端会把它误当成“没有错误”，来源状态就会显示错。
    """

    message = compact_whitespace(str(exc))
    if message:
        return message[:2000]
    return f"{exc.__class__.__name__}：未返回明确错误信息"


class EventNotifier(Protocol):
    """流水线依赖的最小通知接口。"""

    async def notify_event(self, db: Session, event: Event, *, is_update: bool = False) -> bool: ...


class MarketAnalyzer(Protocol):
    provider: str
    model: str

    async def analyze(self, item: RawItem) -> tuple[MarketAnalysis, str | None]: ...


class IntelligencePipeline:
    """串联采集、分析、归并和通知。

    这里保持单一入口，原因是实时采集和历史重建都要走同一套判断逻辑。
    如果两条链路各写一套，事件归并、评分和通知很容易跑出不一致结果。
    """

    def __init__(
        self,
        settings: Settings,
        notifier: EventNotifier | None = None,
        *,
        analyzer_override: MarketAnalyzer | None = None,
        merger_override: EventMerger | None = None,
    ):
        self.settings = settings
        self.notifier = notifier
        self.analyzer_override = analyzer_override
        self.merger_override = merger_override

    def _client(self) -> httpx.AsyncClient:
        """统一 HTTP 客户端配置，避免各采集器和 AI 调用在超时、UA 上漂移。"""

        return httpx.AsyncClient(
            timeout=self.settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.settings.user_agent},
        )

    def _resolve_analyzer(self, client: httpx.AsyncClient) -> MarketAnalyzer:
        if self.analyzer_override is not None:
            return self.analyzer_override
        if self.settings.ai_enabled:
            return OpenAICompatibleAnalyzer(self.settings, client)
        return RuleBasedAnalyzer()

    def _resolve_merger(self, client: httpx.AsyncClient) -> EventMerger:
        if self.merger_override is not None:
            return self.merger_override
        if self.settings.ai_enabled:
            return OpenAICompatibleEventMerger(self.settings, client)
        return NullEventMerger()

    async def poll_source(self, db: Session, source: Source) -> list[Event]:
        """轮询单个来源并处理新内容。"""

        collector = get_collector(source.kind)
        logger.info(
            "开始轮询来源：来源ID=%s，名称=%s，类型=%s，轮询间隔=%s分钟",
            source.id,
            source.name,
            source.kind,
            source.poll_interval_minutes,
        )
        async with self._client() as client:
            result = await collector.collect(source, client)

        source.last_checked_at = utc_now()
        source.last_success_at = utc_now()
        source.last_error = None
        if result.source_fingerprint:
            source.content_fingerprint = result.source_fingerprint
        if result.source_snapshot:
            source.content_snapshot = result.source_snapshot
        db.commit()

        # 首次抓取只建立基线，不直接出事件。这样做是为了避免系统刚启动时把历史存量误判成“刚发生”的情报。
        if result.baseline_only:
            logger.info("来源首次建立正文基线：来源ID=%s，名称=%s", source.id, source.name)
            return []

        events: list[Event] = []
        logger.info(
            "来源采集完成：来源ID=%s，名称=%s，原始条数=%s",
            source.id,
            source.name,
            len(result.items),
        )
        for collected in result.items:
            event = await self.process_item(db, source, collected)
            if event:
                events.append(event)
        logger.info(
            "来源处理完成：来源ID=%s，名称=%s，形成事件=%s",
            source.id,
            source.name,
            len(events),
        )
        return events

    async def process_item(self, db: Session, source: Source, collected: CollectedItem) -> Event | None:
        """将采集到的单条内容入库并推进到分析链路。"""

        canonical_url = canonicalize_url(collected.url)
        title = compact_whitespace(collected.title)
        body = compact_whitespace(collected.body)
        digest = content_hash(title, body)
        existing = db.scalar(
            select(RawItem).where(
                or_(
                    (RawItem.source_id == source.id) & (RawItem.external_id == collected.external_id),
                    RawItem.content_hash == digest,
                )
            )
        )
        if existing:
            logger.debug(
                "跳过重复原始内容：来源ID=%s，外部ID=%s，已存在原始ID=%s，标题=%s",
                source.id,
                collected.external_id,
                existing.id,
                title[:120],
            )
            return existing.evidence.event if existing.evidence else None

        raw = RawItem(
            source_id=source.id,
            external_id=collected.external_id[:500],
            canonical_url=canonical_url,
            title=title,
            body=body,
            author=collected.author,
            published_at=collected.published_at,
            content_hash=digest,
            metadata_json=collected.metadata,
        )
        db.add(raw)
        try:
            db.commit()
        except IntegrityError:
            # 并发轮询或重试下可能碰到唯一键冲突。这里直接跳过，避免把正常幂等场景当成失败。
            db.rollback()
            logger.warning(
                "原始内容写入发生并发冲突，已跳过：来源ID=%s，外部ID=%s，标题=%s",
                source.id,
                collected.external_id,
                title[:120],
            )
            return None

        logger.debug(
            "原始内容已入库：原始ID=%s，来源ID=%s，发布时间=%s，标题=%s",
            raw.id,
            source.id,
            raw.published_at,
            raw.title[:160],
        )
        return await self.process_existing_raw(db, raw, force_reanalyze=True)

    async def process_existing_raw(self, db: Session, raw: RawItem, *, force_reanalyze: bool = False) -> Event | None:
        """基于已落库 raw 重新分析并归并事件。

        这条路径同时服务于实时采集和历史重建，目的是保证两种入口最终产出同一套事件结果。
        """

        source = raw.source or db.get(Source, raw.source_id)
        assert source is not None

        # 先用轻量规则挡掉绝大多数无关内容，减少 AI 调用和事件归并噪声。
        if not is_candidate(raw.title, raw.body, source.kind):
            logger.debug(
                "原始内容被预筛过滤：原始ID=%s，来源ID=%s，原因=未命中市场候选条件，标题=%s",
                raw.id,
                source.id,
                raw.title[:160],
            )
            return None

        analysis = await self.analyze_existing_raw(db, raw, force_reanalyze=force_reanalyze)
        if not analysis.is_market_relevant:
            logger.info(
                "AI 判定为非市场事件：原始ID=%s，来源=%s，事件类型=%s，置信度=%.2f，标题=%s",
                raw.id,
                source.name,
                analysis.event_type,
                analysis.confidence,
                raw.title[:160],
            )
            db.commit()
            return None

        async with self._client() as client:
            merger = self._resolve_merger(client)
            event, is_new, significant_update = await upsert_event(db, source, raw, analysis, merger)
        db.commit()
        logger.info(
            "事件已落库：原始ID=%s，事件ID=%s，新建=%s，重大更新=%s，评级=%s，评分=%s，事件类型=%s，标签=%s，标题=%s",
            raw.id,
            event.id,
            is_new,
            significant_update,
            event.alert_level,
            event.importance_score,
            event.event_type,
            ",".join(analysis.market_signal_tags),
            raw.title[:160],
        )
        if self.notifier and event.alert_level in {"P0", "P1"}:
            await self.notifier.notify_event(db, event, is_update=not is_new and significant_update)
        return event

    async def analyze_existing_raw(
        self,
        db: Session,
        raw: RawItem,
        *,
        force_reanalyze: bool = False,
    ) -> MarketAnalysis:
        """分析已存在的 raw，并在需要时覆盖旧分析。"""

        if raw.analysis and not force_reanalyze:
            try:
                logger.debug("复用已有分析结果：原始ID=%s，分析器=%s", raw.id, raw.analysis.provider)
                return MarketAnalysis.model_validate(raw.analysis.result_json)
            except ValidationError:
                # 历史分析结果可能落后于新 schema。这里主动重跑，避免旧数据把新流程卡死。
                pass

        async with self._client() as client:
            analyzer = self._resolve_analyzer(client)
            try:
                logger.debug(
                    "开始分析原始内容：原始ID=%s，分析器=%s，模型=%s，标题=%s",
                    raw.id,
                    analyzer.provider,
                    analyzer.model,
                    raw.title[:160],
                )
                analysis, raw_response = await analyzer.analyze(raw)
            except AIAnalysisError:
                # AI 失败时退回规则分析，原因是首版系统更重视链路可用性，而不是每次都必须拿到模型结果。
                logger.warning(
                    "AI 分析失败，回退到规则分析：原始ID=%s，标题=%s",
                    raw.id,
                    raw.title[:160],
                )
                analyzer = RuleBasedAnalyzer()
                analysis, raw_response = await analyzer.analyze(raw)

        existing = raw.analysis or db.scalar(select(Analysis).where(Analysis.raw_item_id == raw.id))
        if existing:
            existing.provider = analyzer.provider
            existing.model = analyzer.model
            existing.prompt_version = PROMPT_VERSION
            existing.result_json = analysis.model_dump()
            existing.raw_response = raw_response
        else:
            db.add(
                Analysis(
                    raw_item_id=raw.id,
                    provider=analyzer.provider,
                    model=analyzer.model,
                    prompt_version=PROMPT_VERSION,
                    result_json=analysis.model_dump(),
                    raw_response=raw_response,
                )
            )
        db.commit()
        db.refresh(raw)
        logger.debug(
            "分析结果已保存：原始ID=%s，分析器=%s，模型=%s，市场相关=%s，事件类型=%s，影响范围=%s，强度=%s，置信度=%.2f，标签=%s",
            raw.id,
            analyzer.provider,
            analyzer.model,
            analysis.is_market_relevant,
            analysis.event_type,
            analysis.impact_scope,
            analysis.impact_strength,
            analysis.confidence,
            ",".join(analysis.market_signal_tags),
        )
        return analysis

    async def rebuild_events_from_raw(self, db: Session, *, force_reanalyze: bool = True) -> dict[str, int]:
        """基于现有 raw_items 重建 events。"""

        # 重建时清空派生表，但保留 raw 采集数据。这样既能修复错误聚合，也不会丢掉原始审计轨迹。
        db.execute(delete(Notification))
        db.execute(delete(DailyReport))
        db.execute(delete(EventEvidence))
        db.execute(delete(Event))
        if force_reanalyze:
            db.execute(delete(Analysis))
        db.commit()
        logger.info("开始基于原始数据重建事件：重新分析=%s", force_reanalyze)

        raws = db.scalars(
            select(RawItem)
            .options(selectinload(RawItem.source), selectinload(RawItem.analysis))
            .order_by(RawItem.published_at.asc(), RawItem.id.asc())
        ).all()

        stats = {"raws": len(raws), "analyzed": 0, "market_relevant": 0, "events": 0}
        for raw in raws:
            source = raw.source or db.get(Source, raw.source_id)
            assert source is not None
            if not is_candidate(raw.title, raw.body, source.kind):
                continue
            analysis = await self.analyze_existing_raw(db, raw, force_reanalyze=force_reanalyze)
            stats["analyzed"] += 1
            if not analysis.is_market_relevant:
                continue
            stats["market_relevant"] += 1
            await self.process_existing_raw(db, raw, force_reanalyze=False)
        stats["events"] = db.scalar(select(func.count()).select_from(Event))
        logger.info("事件重建完成：统计=%s", stats)
        return stats

    async def poll_due_sources(self, db: Session) -> dict[str, int]:
        """轮询当前所有到期来源。"""

        now = utc_now()
        sources = db.scalars(select(Source).where(Source.enabled.is_(True))).all()
        result = {"checked": 0, "events": 0, "failed": 0}
        logger.debug("扫描待轮询来源：启用来源数=%s", len(sources))
        for source in sources:
            last_checked = source.last_checked_at
            if last_checked is not None and last_checked.tzinfo is None:
                last_checked = last_checked.replace(tzinfo=now.tzinfo)
            due = last_checked is None or last_checked <= now - timedelta(minutes=source.poll_interval_minutes)
            if not due:
                continue
            result["checked"] += 1
            try:
                events = await self.poll_source(db, source)
                result["events"] += len(events)
            except Exception as exc:
                # 单个来源失败不能中断整轮轮询，否则一个坏来源会把全部情报采集拖停。
                db.rollback()
                tracked_source = db.get(Source, source.id)
                if tracked_source:
                    tracked_source.last_checked_at = utc_now()
                    tracked_source.last_error = describe_exception(exc)
                    db.commit()
                logger.exception(
                    "来源轮询失败：来源ID=%s，名称=%s，类型=%s，错误=%s",
                    source.id,
                    source.name,
                    source.kind,
                    exc,
                )
                result["failed"] += 1
        logger.info("本轮来源轮询结束：结果=%s", result)
        return result
