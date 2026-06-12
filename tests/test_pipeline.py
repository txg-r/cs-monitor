from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.models import Event, RawItem, Source
from app.schemas import CollectedItem, EventMergeCandidate, EventMergeDecision, MarketAnalysis
from app.services.pipeline import IntelligencePipeline


class AlwaysMergeMerger:
    """测试桩：强制把候选视为同一事件。"""

    async def compare(
        self,
        candidate: EventMergeCandidate,
        item: RawItem,
        analysis: MarketAnalysis,
    ) -> EventMergeDecision:
        return EventMergeDecision(same_event=True, confidence=0.95, reason="测试桩强制合并")


@pytest.mark.asyncio
async def test_pipeline_deduplicates_exact_item(db):
    """验证调度重试同一条外部 ID 时不会重复保存原文或触发第二次事件处理。"""

    source = Source(name="官方", kind="steam_news", url="https://example.com", credibility=100, config={})
    db.add(source)
    db.commit()
    item = CollectedItem(
        external_id="gid-1",
        url="https://example.com/update?utm_source=x",
        title="New Case Released",
        body="A new case is now in the drop pool.",
        published_at=datetime.now(timezone.utc),
    )
    pipeline = IntelligencePipeline(Settings(scheduler_enabled=False, ai_enabled=False))
    first = await pipeline.process_item(db, source, item)
    second = await pipeline.process_item(db, source, item)
    assert first is not None
    assert second.id == first.id
    assert db.scalar(select(func.count()).select_from(RawItem)) == 1


@pytest.mark.asyncio
async def test_pipeline_aggregates_same_event_only_when_merger_confirms(db):
    """验证跨来源归并不再靠标题相似，而是依赖明确的归并判定结果。"""

    official = Source(name="官方", kind="steam_news", url="https://a.example", credibility=100, config={})
    media = Source(name="媒体", kind="rss", url="https://b.example", credibility=75, config={})
    db.add_all([official, media])
    db.commit()
    pipeline = IntelligencePipeline(
        Settings(scheduler_enabled=False, ai_enabled=False),
        merger_override=AlwaysMergeMerger(),
    )
    first = await pipeline.process_item(
        db,
        official,
        CollectedItem(
            external_id="a",
            url="https://a.example/1",
            title="CS2 Update New Weapon Case Released",
            body="new case entered the drop pool",
            published_at=datetime.now(timezone.utc),
        ),
    )
    second = await pipeline.process_item(
        db,
        media,
        CollectedItem(
            external_id="b",
            url="https://b.example/2",
            title="Breaking CS2 Update New Weapon Case Released",
            body="community discusses the new case drop pool",
            published_at=datetime.now(timezone.utc),
        ),
    )
    assert first.id == second.id
    assert db.scalar(select(func.count()).select_from(Event)) == 1
    assert second.evidence_count == 2


@pytest.mark.asyncio
async def test_pipeline_does_not_chain_merge_far_apart_updates(db):
    """验证相隔很久的通用官方更新标题不会仅因同名而被串到同一事件。"""

    source = Source(name="官方", kind="steam_news", url="https://example.com", credibility=100, config={})
    db.add(source)
    db.commit()
    pipeline = IntelligencePipeline(
        Settings(scheduler_enabled=False, ai_enabled=False),
        merger_override=AlwaysMergeMerger(),
    )
    first = await pipeline.process_item(
        db,
        source,
        CollectedItem(
            external_id="march",
            url="https://example.com/march",
            title="Counter-Strike 2 Update",
            body="New collection entered the weekly drop pool.",
            published_at=datetime(2026, 3, 11, tzinfo=timezone.utc),
        ),
    )
    second = await pipeline.process_item(
        db,
        source,
        CollectedItem(
            external_id="june",
            url="https://example.com/june",
            title="Counter-Strike 2 Update",
            body="Added display of lowest and highest sticker price in the last 7 days in the Major Shop.",
            published_at=datetime(2026, 6, 10, tzinfo=timezone.utc),
        ),
    )
    assert first.id != second.id
    assert db.scalar(select(func.count()).select_from(Event)) == 2


@pytest.mark.asyncio
async def test_rebuild_events_reanalyzes_and_recreates_event_view(db):
    """验证重建流程会按 raw 发布时间重放事件，而不是沿用被污染的旧 event 归并结果。"""

    source = Source(name="官方", kind="steam_news", url="https://example.com", credibility=100, config={})
    db.add(source)
    db.commit()
    pipeline = IntelligencePipeline(Settings(scheduler_enabled=False, ai_enabled=False))
    await pipeline.process_item(
        db,
        source,
        CollectedItem(
            external_id="a",
            url="https://example.com/a",
            title="Counter-Strike 2 Update",
            body="New collection entered the weekly drop pool.",
            published_at=datetime(2026, 3, 11, tzinfo=timezone.utc),
        ),
    )
    await pipeline.process_item(
        db,
        source,
        CollectedItem(
            external_id="b",
            url="https://example.com/b",
            title="Counter-Strike 2 Update",
            body="Trade offers containing Counter-Strike 2 items are now limited to 1,000 items.",
            published_at=datetime(2026, 4, 22, tzinfo=timezone.utc),
        ),
    )

    rebuilt = IntelligencePipeline(
        Settings(scheduler_enabled=False, ai_enabled=False),
        merger_override=AlwaysMergeMerger(),
    )
    stats = await rebuilt.rebuild_events_from_raw(db, force_reanalyze=True)
    assert stats["raws"] == 2
    assert db.scalar(select(func.count()).select_from(Event)) == 2
